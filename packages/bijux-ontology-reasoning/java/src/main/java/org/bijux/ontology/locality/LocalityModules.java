package org.bijux.ontology.locality;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.*;
import org.bijux.ontology.axioms.AxiomConstruction;
import org.bijux.ontology.axioms.AxiomProjection;
import org.bijux.ontology.backend.WorkerProtocol;
import org.bijux.ontology.documents.NativeDocuments;
import org.semanticweb.owlapi.apibinding.OWLManager;
import org.semanticweb.owlapi.model.*;
import uk.ac.manchester.cs.owlapi.modularity.ModuleType;
import uk.ac.manchester.cs.owlapi.modularity.SyntacticLocalityModuleExtractor;

/** Maintained syntactic modules preserve original theory ownership and exact source axioms. */
public final class LocalityModules {
    private LocalityModules() {}

    private static OWLEntity entity(JsonNode record, OWLDataFactory factory, int cap) throws IOException {
        if (!WorkerProtocol.text(record.path("kind"), 32).equals("entity"))
            throw new IOException("Typed signature entity required");
        IRI iri = IRI.create(WorkerProtocol.text(record.path("iri"), cap));
        if (!iri.isAbsolute()) throw new IOException("Absolute signature entity required");
        switch (WorkerProtocol.text(record.path("role"), 32)) {
            case "class": return factory.getOWLClass(iri);
            case "object_property": return factory.getOWLObjectProperty(iri);
            case "data_property": return factory.getOWLDataProperty(iri);
            case "annotation_property": return factory.getOWLAnnotationProperty(iri);
            case "datatype": return factory.getOWLDatatype(iri);
            case "named_individual": return factory.getOWLNamedIndividual(iri);
            default: throw new IOException("Unsupported signature entity role");
        }
    }

    /** Derive new standalone bytes, never edit the loaded original ontology closure. */
    public static Map<String,Object> evaluate(JsonNode request, Path job, Map<String,Object> result) throws Exception {
        int cap = WorkerProtocol.positive(request.path("max_bytes"));
        int maximum = WorkerProtocol.positive(request.path("max_nodes"));
        int issueCap = WorkerProtocol.positive(request.path("max_issues"));
        int depth = WorkerProtocol.positive(request.path("projection_depth"));
        String selected = WorkerProtocol.text(request.path("method"), 16);
        ModuleType method = ModuleType.valueOf(selected);
        result.put("method", selected); result.put("qualified", false);
        result.put("source_unsupported", Collections.emptyList());
        List<String> failures = new ArrayList<>(); result.put("failures", failures);
        NativeDocuments.Loaded input = NativeDocuments.loadRequest(request, job);
        List<Map<String,Object>> losses = new ArrayList<>();
        input.qualifyOriginals(losses, cap, issueCap);
        if (!losses.isEmpty()) { failures.add("Original native source qualification has losses"); return result; }
        OWLOntology original = input.ontology;
        OWLDataFactory factory = original.getOWLOntologyManager().getOWLDataFactory();
        Map<Integer,OWLOntology> sources = new TreeMap<>();
        for (OWLOntology source : original.getImportsClosure()) {
            Integer index = input.owners.get(original.getOWLOntologyManager().getOntologyDocumentIRI(source).toString());
            if (index == null || sources.put(index, source) != null) throw new IOException("Unowned source ontology");
        }
        Set<OWLAxiom> all = new LinkedHashSet<>();
        for (OWLOntology source : sources.values()) for (OWLAxiom axiom : source.getAxioms()) {
            if (all.size() >= maximum && !all.contains(axiom)) throw new WorkerProtocol.OutputLimit();
            all.add(axiom);
        }
        Map<String,Object> projected = AxiomProjection.projectChanges(all, maximum, depth, cap);
        result.put("source_unsupported", projected.get("unsupported"));
        if (!((Collection<?>) projected.get("unsupported")).isEmpty()) return result;
        JsonNode requested = request.path("signature");
        if (!requested.isArray() || requested.size() == 0 || requested.size() > maximum)
            throw new IOException("Invalid finite signature");
        Set<OWLEntity> signature = new LinkedHashSet<>();
        for (JsonNode record : requested) {
            OWLEntity term = entity(record, factory, cap);
            if (!signature.add(term)) throw new IOException("Duplicate signature entity");
            boolean found = term.isBuiltIn();
            for (OWLOntology source : sources.values()) found |= source.containsEntityInSignature(term);
            if (!found) {
                if (failures.size() >= issueCap) throw new WorkerProtocol.OutputLimit();
                failures.add("Missing typed signature entity: " + term.getEntityType().getName() + " " + term.getIRI());
            }
        }
        if (!failures.isEmpty()) return result;
        AxiomConstruction construction = new AxiomConstruction(factory, maximum, depth, cap);
        JsonNode queries = request.path("consumer_entailments");
        if (!queries.isArray() || queries.size() == 0 || queries.size() > maximum)
            throw new IOException("Nonempty consumer entailments required");
        for (JsonNode query : queries) for (OWLEntity term : construction.axiom(query).getSignature())
            if (!term.isBuiltIn() && !signature.contains(term)) {
                if (failures.size() >= issueCap) throw new WorkerProtocol.OutputLimit();
                failures.add("Consumer entailment uses a role outside the selected signature: " + term);
            }
        if (!failures.isEmpty()) return result;
        IRI identity = IRI.create(WorkerProtocol.text(request.path("module_iri"), cap));
        if (!identity.isAbsolute()) throw new IOException("Absolute module identity required");
        for (OWLOntology source : sources.values()) {
            if (source.getOntologyID().getOntologyIRI().filter(identity::equals).isPresent()
                    || source.getOntologyID().getVersionIRI().filter(identity::equals).isPresent()
                    || original.getOWLOntologyManager().getOntologyDocumentIRI(source).equals(identity)
                    || source.containsEntityInSignature(identity)) {
                failures.add("The derived module identity is not fresh"); return result;
            }
        }
        Set<OWLAxiom> extracted = new SyntacticLocalityModuleExtractor(
            OWLManager.createOWLOntologyManager(), all.stream(), method).extract(signature);
        if (extracted.size() > maximum) throw new WorkerProtocol.OutputLimit();
        List<Map<String,Object>> records = new ArrayList<>();
        for (OWLAxiom axiom : extracted) {
            List<Integer> owners = new ArrayList<>();
            for (Map.Entry<Integer,OWLOntology> entry : sources.entrySet())
                if (entry.getValue().containsAxiom(axiom)) owners.add(entry.getKey());
            if (owners.isEmpty()) throw new IOException("Extracted axiom has no original structural owner");
            Map<String,Object> record = new LinkedHashMap<>(); record.put("documents", owners);
            record.put("projection", AxiomProjection.projectChanges(Collections.singleton(axiom), maximum, depth, cap));
            records.add(record);
        }
        result.put("axioms", records);
        OWLOntologyManager outputManager = OWLManager.createOWLOntologyManager();
        OWLOntology module = outputManager.createOntology(extracted, identity);
        for (OWLAnnotation annotation : original.getAnnotations())
            outputManager.applyChange(new AddOntologyAnnotation(module, annotation));
        Path output = job.resolve("candidate.owl");
        NativeDocuments.checkedWriteIsolated(module, output, cap, issueCap);
        result.put("bytes", Files.size(output)); result.put("qualified", true);
        return result;
    }
}
