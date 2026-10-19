package org.bijux.ontology.backend;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.*;
import java.nio.file.Path;
import java.util.*;
import org.bijux.ontology.documents.NativeDocuments;
import org.semanticweb.HermiT.Configuration;
import org.semanticweb.HermiT.ReasonerFactory;
import org.semanticweb.owlapi.model.*;
import org.semanticweb.owlapi.profiles.*;
import org.semanticweb.owlapi.reasoner.*;

/** One qualified original closure and strict mature reasoner for an owned request. */
public final class QualifiedReasoning implements AutoCloseable {
    public final OWLOntology ontology;
    public final OWLReasoner reasoner;
    private QualifiedReasoning(OWLOntology ontology, OWLReasoner reasoner) {
        this.ontology = ontology; this.reasoner = reasoner;
    }

    private static String artifactVersion() throws IOException {
        String name = "META-INF/maven/net.sourceforge.owlapi/org.semanticweb.hermit/pom.properties";
        Enumeration<java.net.URL> resources = ReasonerFactory.class.getClassLoader().getResources(name);
        if (!resources.hasMoreElements()) throw new IOException("Missing reasoner artifact identity");
        Properties properties = new Properties();
        try (InputStream stream = resources.nextElement().openStream()) { properties.load(stream); }
        if (resources.hasMoreElements() || !"1.4.5.519".equals(properties.getProperty("version")))
            throw new IOException("Unqualified reasoner artifact identity");
        return properties.getProperty("version");
    }

    /** Share actual parser/profile/backend gates without another parse or engine. */
    public static QualifiedReasoning open(JsonNode request, Path job, Map<String,Object> result)
            throws Exception {
        int cap = WorkerProtocol.positive(request.path("max_bytes"));
        int resources = WorkerProtocol.positive(request.path("max_resources"));
        int issueCap = WorkerProtocol.positive(request.path("max_issues"));
        List<Map<String,Object>> changes = new ArrayList<>();
        List<String> violations = new ArrayList<>();
        result.put("changes", changes); result.put("violations", violations);
        result.put("qualified", false); result.put("consistent", null);
        result.put("dl_profile", null); result.put("reasoner", null); result.put("error", null);
        String kind = WorkerProtocol.text(request.path("input_kind"), 32);
        NativeDocuments.Loaded input;
        if (kind.equals("native")) input = NativeDocuments.loadRequest(request, job);
        else if (kind.equals("rdf")) input = NativeDocuments.loadRdfRequest(request, job);
        else throw new IOException("Unsupported diagnosis input kind");
        result.put("input_kind", kind); result.put("loaders", input.loaders);
        result.put("documents", input.records(resources));
        input.qualifyOriginals(changes, cap, issueCap);
        if (!changes.isEmpty()) return null;
        for (Map<String,Object> loader : input.loaders) {
            if (!Boolean.TRUE.equals(loader.get("present"))
                || ((Number) loader.get("unparsed")).longValue() != 0
                || ((Number) loader.get("guessed")).longValue() != 0) return null;
        }
        OWLOntology ontology = input.ontology;
        OWLProfileReport profile = new OWL2DLProfile().checkOntology(ontology);
        result.put("dl_profile", profile.isInProfile());
        for (OWLProfileViolation violation : profile.getViolations()) {
            if (violations.size() >= issueCap) throw new WorkerProtocol.OutputLimit();
            violations.add(WorkerProtocol.rendering(violation, cap));
        }
        if (!profile.isInProfile()) return null;
        // Rules are not part of this OWL 2 DL operation, even when accepted by
        // an OWLAPI profile implementation or partially supported by HermiT.
        for (OWLOntology member : ontology.getImportsClosure())
            if (member.axioms(AxiomType.SWRL_RULE).findAny().isPresent())
                throw new IOException("SWRL rules are outside this DL diagnosis profile");
        Configuration configuration = new Configuration();
        configuration.ignoreUnsupportedDatatypes = false;
        configuration.throwInconsistentOntologyException = false;
        configuration.freshEntityPolicy = FreshEntityPolicy.DISALLOW;
        configuration.individualTaskTimeout = WorkerProtocol.positive(request.path("task_timeout_ms"));
        configuration.warningMonitor = warning -> { throw new IllegalArgumentException("Unqualified native reasoner warning"); };
        String artifact = artifactVersion();
        OWLReasoner reasoner = new ReasonerFactory().createReasoner(ontology, configuration);
        try {
            Map<String,Object> identity = new LinkedHashMap<>();
            identity.put("name", reasoner.getReasonerName());
            identity.put("artifact_version", artifact);
            identity.put("native_version", reasoner.getReasonerVersion().toString());
            identity.put("ignore_unsupported_datatypes", configuration.ignoreUnsupportedDatatypes);
            result.put("reasoner", identity);
            if (!"HermiT".equals(identity.get("name")) || !"1.4.1.513".equals(identity.get("native_version")))
                throw new IOException("Unsupported native reasoner identity");
            result.put("qualified", true);
            return new QualifiedReasoning(ontology, reasoner);
        } catch (Exception | Error failure) { reasoner.dispose(); throw failure; }
    }

    @Override public void close() { reasoner.dispose(); }
}
