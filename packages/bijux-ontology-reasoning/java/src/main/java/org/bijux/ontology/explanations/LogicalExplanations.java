package org.bijux.ontology.explanations;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.*;
import org.bijux.ontology.axioms.AxiomConstruction;
import org.bijux.ontology.backend.QualifiedReasoning;
import org.bijux.ontology.backend.WorkerProtocol;
import org.bijux.ontology.documents.NativeDocuments;
import org.semanticweb.HermiT.ReasonerFactory;
import org.semanticweb.owl.explanation.api.*;
import org.semanticweb.owl.explanation.impl.blackbox.checker.InconsistentOntologyExplanationGeneratorFactory;
import org.semanticweb.owlapi.apibinding.OWLManager;
import org.semanticweb.owlapi.model.*;
import org.semanticweb.owlapi.reasoner.*;

/** One maintained justification, with original occurrences and independent replay. */
public final class LogicalExplanations {
    private LogicalExplanations() {}

    private static String generatorVersion() throws IOException {
        String resource = "META-INF/maven/net.sourceforge.owlapi/owlexplanation/pom.properties";
        Enumeration<java.net.URL> sources = ExplanationManager.class.getClassLoader().getResources(resource);
        if (!sources.hasMoreElements()) throw new IOException("Missing explanation artifact identity");
        Properties properties = new Properties();
        try (InputStream input = sources.nextElement().openStream()) { properties.load(input); }
        if (sources.hasMoreElements() || !"5.0.1".equals(properties.getProperty("version")))
            throw new IOException("Unqualified explanation artifact identity");
        return properties.getProperty("version");
    }

    private static String digest(String value) throws Exception {
        byte[] bytes = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
        StringBuilder output = new StringBuilder(64);
        for (byte item : bytes) output.append(String.format("%02x", item & 255));
        return output.toString();
    }

    private static Map<String,Object> rendering(OWLAxiom axiom, int cap) throws Exception {
        String text = WorkerProtocol.rendering(axiom, cap);
        Map<String,Object> result = new LinkedHashMap<>();
        result.put("rendering", text); result.put("sha256", digest(text));
        return result;
    }

    /** Never substitute an empty support set for failed native explanation work. */
    public static Map<String,Object> evaluate(JsonNode request, Path job,
            Map<String,Object> result) throws Exception {
        int cap = WorkerProtocol.positive(request.path("max_bytes"));
        int maximum = WorkerProtocol.positive(request.path("max_nodes"));
        int maxAxioms = WorkerProtocol.positive(request.path("max_axioms"));
        int depth = WorkerProtocol.positive(request.path("query_depth"));
        int milliseconds = WorkerProtocol.positive(request.path("task_timeout_ms"));
        long deadline = System.nanoTime() + milliseconds * 1000000L;
        result.put("generator_version", generatorVersion());
        result.put("reproduced", false); result.put("support", null);
        result.put("minimality", "neither"); result.put("enumeration_complete", false);
        try (QualifiedReasoning input = QualifiedReasoning.open(request, job, result)) {
            if (input == null) return result;
            boolean consistent = input.reasoner.isConsistent(); result.put("consistent", consistent);
            OWLDataFactory factory = input.ontology.getOWLOntologyManager().getOWLDataFactory();
            JsonNode selected = request.path("question");
            String kind = WorkerProtocol.text(selected.path("kind"), 32);
            OWLAxiom query;
            boolean applicable;
            if (kind.equals("entailment")) {
                query = new AxiomConstruction(factory, maximum, depth, cap).axiom(selected.path("axiom"));
                applicable = consistent && input.reasoner.isEntailed(query);
            } else if (kind.equals("unsatisfiability")) {
                IRI iri = IRI.create(WorkerProtocol.text(selected.path("class_iri"), cap));
                if (!input.ontology.containsClassInSignature(iri, org.semanticweb.owlapi.model.parameters.Imports.INCLUDED))
                    throw new IOException("Unloaded selected class");
                OWLClass cls = factory.getOWLClass(iri);
                query = factory.getOWLSubClassOfAxiom(cls, factory.getOWLNothing());
                applicable = consistent && !input.reasoner.isSatisfiable(cls);
            } else if (kind.equals("inconsistency")) {
                query = factory.getOWLSubClassOfAxiom(factory.getOWLThing(), factory.getOWLNothing());
                applicable = !consistent;
            } else throw new IOException("Unsupported explanation question");
            result.put("applicable", applicable);
            if (!applicable) return result;
            ReasonerFactory reasoners = new ReasonerFactory();
            ExplanationGeneratorFactory<OWLAxiom> generators = kind.equals("inconsistency")
                ? new InconsistentOntologyExplanationGeneratorFactory(reasoners, factory,
                    OWLManager::createOWLOntologyManager, (long) milliseconds)
                : ExplanationManager.createExplanationGeneratorFactory(reasoners, OWLManager::createOWLOntologyManager);
            ExplanationProgressMonitor<OWLAxiom> monitor = new ExplanationProgressMonitor<OWLAxiom>() {
                @Override public boolean isCancelled() { return System.nanoTime() >= deadline; }
                @Override public void foundExplanation(ExplanationGenerator<OWLAxiom> generator,
                        Explanation<OWLAxiom> explanation, Set<Explanation<OWLAxiom>> all) {}
            };
            Set<Explanation<OWLAxiom>> generated = generators.createExplanationGenerator(input.ontology, monitor)
                .getExplanations(query, 1);
            if (System.nanoTime() >= deadline) throw new WorkerProtocol.OutputLimit();
            if (generated.size() != 1) throw new IOException("No complete selected justification returned");
            Set<OWLAxiom> support = generated.iterator().next().getAxioms();
            if (support.size() > maxAxioms || support.size() > maximum) throw new WorkerProtocol.OutputLimit();
            Map<String,Integer> owners = new HashMap<>();
            JsonNode documents = request.path("documents");
            for (int i = 0; i < documents.size(); i++)
                owners.put(WorkerProtocol.text(documents.get(i).path("effective_url"), cap), i);
            List<Map<String,Object>> records = new ArrayList<>();
            for (OWLAxiom axiom : support) {
                if (axiom.anonymousIndividuals().findAny().isPresent())
                    throw new IOException("Anonymous support correspondence unavailable");
                Map<String,Object> record = rendering(axiom, cap);
                List<Map<String,Object>> occurrences = new ArrayList<>();
                for (OWLOntology member : input.ontology.getImportsClosure()) {
                    Integer index = owners.get(member.getOWLOntologyManager().getOntologyDocumentIRI(member).toString());
                    if (index == null) throw new IOException("Unowned support source");
                    for (OWLAxiom original : member.getAxioms())
                        if (original.getAxiomWithoutAnnotations().equals(axiom.getAxiomWithoutAnnotations())) {
                            if (occurrences.size() >= maximum) throw new WorkerProtocol.OutputLimit();
                            Map<String,Object> occurrence = rendering(original, cap);
                            occurrence.put("document", index); occurrences.add(occurrence);
                        }
                }
                if (occurrences.isEmpty()) throw new IOException("Support axiom lacks an original occurrence");
                record.put("origins", occurrences); records.add(record);
            }
            Set<OWLAxiom> standalone = new LinkedHashSet<>(support);
            Set<OWLEntity> signature = new LinkedHashSet<>();
            support.forEach(axiom -> signature.addAll(axiom.getSignature()));
            List<String> declarations = new ArrayList<>();
            for (OWLEntity entity : signature) if (!entity.isBuiltIn()) {
                if (declarations.size() >= maximum) throw new WorkerProtocol.OutputLimit();
                OWLDeclarationAxiom declaration = factory.getOWLDeclarationAxiom(entity);
                standalone.add(declaration); declarations.add(WorkerProtocol.rendering(declaration, cap));
            }
            OWLOntology proof = OWLManager.createOWLOntologyManager().createOntology(standalone, IRI.create("urn:bijux:ontology:justification"));
            Path output = job.resolve("justification.owl");
            OWLOntology reread = NativeDocuments.checkedWriteIsolated(proof, output, cap, maximum);
            OWLReasoner replay = reasoners.createReasoner(reread);
            boolean reproduced;
            try { reproduced = kind.equals("inconsistency") ? !replay.isConsistent()
                : replay.isConsistent() && replay.isEntailed(query); }
            finally { replay.dispose(); }
            if (!reproduced) throw new IOException("Standalone support does not reproduce the observation");
            result.put("support", records); result.put("declarations", declarations);
            result.put("proof_bytes", Files.size(output)); result.put("reproduced", true);
        } catch (TimeOutException | WorkerProtocol.OutputLimit exhausted) { result.put("status", "exhausted"); }
        catch (OutOfMemoryError | StackOverflowError exhausted) { result.put("status", "exhausted"); }
        catch (Exception unsupported) { result.put("error", unsupported.getClass().getSimpleName()); }
        return result;
    }
}
