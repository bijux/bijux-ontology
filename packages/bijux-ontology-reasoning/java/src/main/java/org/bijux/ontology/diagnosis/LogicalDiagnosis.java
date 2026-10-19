package org.bijux.ontology.diagnosis;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.*;
import java.nio.file.Path;
import java.util.*;
import org.bijux.ontology.backend.WorkerProtocol;
import org.bijux.ontology.documents.NativeDocuments;
import org.semanticweb.HermiT.Configuration;
import org.semanticweb.HermiT.ReasonerFactory;
import org.semanticweb.owlapi.model.*;
import org.semanticweb.owlapi.model.parameters.Imports;
import org.semanticweb.owlapi.profiles.*;
import org.semanticweb.owlapi.reasoner.*;

/** Actual mature DL queries over the same owned and qualified input closure. */
public final class LogicalDiagnosis {
    private LogicalDiagnosis() {}

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

    /** Bound inventories, retain completed native answers, and never infer from absence. */
    public static Map<String,Object> evaluate(JsonNode request, Path job,
            Map<String,Object> result) throws Exception {
        int cap = WorkerProtocol.positive(request.path("max_bytes"));
        int maximum = WorkerProtocol.positive(request.path("max_nodes"));
        int resources = WorkerProtocol.positive(request.path("max_resources"));
        int issueCap = WorkerProtocol.positive(request.path("max_issues"));
        List<Map<String,Object>> classes = new ArrayList<>();
        List<Map<String,Object>> changes = new ArrayList<>();
        List<String> violations = new ArrayList<>();
        List<String> selected = new ArrayList<>();
        result.put("classes", classes); result.put("changes", changes);
        result.put("violations", violations); result.put("selected", selected);
        result.put("qualified", false); result.put("consistent", null);
        result.put("dl_profile", null); result.put("reasoner", null);
        result.put("error", null);
        OWLReasoner reasoner = null;
        try {
            String kind = WorkerProtocol.text(request.path("input_kind"), 32);
            NativeDocuments.Loaded input;
            if (kind.equals("native")) input = NativeDocuments.loadRequest(request, job);
            else if (kind.equals("rdf")) input = NativeDocuments.loadRdfRequest(request, job);
            else throw new IOException("Unsupported diagnosis input kind");
            result.put("input_kind", kind); result.put("loaders", input.loaders);
            result.put("documents", input.records(resources));
            input.qualifyOriginals(changes, cap, issueCap);
            if (!changes.isEmpty()) return result;
            for (Map<String,Object> loader : input.loaders) {
                if (!Boolean.TRUE.equals(loader.get("present"))
                    || ((Number) loader.get("unparsed")).longValue() != 0
                    || ((Number) loader.get("guessed")).longValue() != 0) return result;
            }
            OWLOntology ontology = input.ontology;
            OWLProfileReport profile = new OWL2DLProfile().checkOntology(ontology);
            result.put("dl_profile", profile.isInProfile());
            for (OWLProfileViolation violation : profile.getViolations()) {
                if (violations.size() >= issueCap) throw new WorkerProtocol.OutputLimit();
                violations.add(WorkerProtocol.rendering(violation, cap));
            }
            if (!profile.isInProfile()) return result;
            // Rules are not part of this OWL 2 DL operation, even when accepted by
            // an OWLAPI profile implementation or partially supported by HermiT.
            for (OWLOntology member : ontology.getImportsClosure())
                if (member.axioms(AxiomType.SWRL_RULE).findAny().isPresent())
                    throw new IOException("SWRL rules are outside this DL diagnosis profile");
            JsonNode selection = request.path("classes");
            if (selection.isNull()) {
                TreeSet<String> inventory = new TreeSet<>();
                Iterator<OWLClass> iterator = ontology.classesInSignature(Imports.INCLUDED).iterator();
                while (iterator.hasNext()) {
                    String iri = WorkerProtocol.rendering(iterator.next().getIRI(), cap);
                    if (inventory.size() >= maximum && !inventory.contains(iri))
                        throw new WorkerProtocol.OutputLimit();
                    inventory.add(iri);
                }
                selected.addAll(inventory);
            } else {
                if (!selection.isArray() || selection.size() > maximum)
                    throw new WorkerProtocol.OutputLimit();
                Set<String> seen = new HashSet<>();
                for (JsonNode value : selection) {
                    String iri = WorkerProtocol.text(value, cap);
                    if (!seen.add(iri) || !ontology.containsClassInSignature(IRI.create(iri), Imports.INCLUDED))
                        throw new IOException("Requested class lacks selected signature coverage");
                    selected.add(iri);
                }
            }
            Configuration configuration = new Configuration();
            configuration.ignoreUnsupportedDatatypes = false;
            configuration.throwInconsistentOntologyException = false;
            configuration.freshEntityPolicy = FreshEntityPolicy.DISALLOW;
            configuration.individualTaskTimeout = WorkerProtocol.positive(request.path("task_timeout_ms"));
            configuration.warningMonitor = warning -> { throw new IllegalArgumentException("Unqualified native reasoner warning"); };
            String artifact = artifactVersion();
            reasoner = new ReasonerFactory().createReasoner(ontology, configuration);
            Map<String,Object> identity = new LinkedHashMap<>();
            identity.put("name", reasoner.getReasonerName());
            identity.put("artifact_version", artifact);
            identity.put("native_version", reasoner.getReasonerVersion().toString());
            identity.put("ignore_unsupported_datatypes", configuration.ignoreUnsupportedDatatypes);
            result.put("reasoner", identity);
            if (!"HermiT".equals(identity.get("name")) || !"1.4.1.513".equals(identity.get("native_version")))
                throw new IOException("Unsupported native reasoner identity");
            result.put("qualified", true);
            boolean consistent = reasoner.isConsistent();
            result.put("consistent", consistent);
            if (consistent) {
                for (String iri : selected) {
                    if (classes.size() >= maximum) throw new WorkerProtocol.OutputLimit();
                    boolean satisfiable = reasoner.isSatisfiable(ontology.getOWLOntologyManager()
                        .getOWLDataFactory().getOWLClass(IRI.create(iri)));
                    Map<String,Object> item = new LinkedHashMap<>();
                    item.put("iri", iri); item.put("satisfiable", satisfiable); classes.add(item);
                }
            }
        } catch (TimeOutException | WorkerProtocol.OutputLimit exhausted) {
            result.put("status", "exhausted");
        } catch (OutOfMemoryError | StackOverflowError exhausted) {
            result.put("status", "exhausted");
        } catch (Exception unsupported) {
            result.put("qualified", false);
            result.put("error", unsupported.getClass().getSimpleName());
        } finally {
            if (reasoner != null) reasoner.dispose();
        }
        return result;
    }
}
