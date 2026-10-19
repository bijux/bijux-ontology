package org.bijux.ontology.axioms;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.IOException;
import java.nio.file.Path;
import java.util.*;
import org.bijux.ontology.backend.WorkerProtocol;
import org.bijux.ontology.documents.NativeDocuments;
import org.semanticweb.owlapi.model.*;

/** Root-only structural transactions preserve read-only imported modules. */
public final class AxiomChanges {
    private AxiomChanges() {}
    private static Set<OWLAxiom> construct(JsonNode values, AxiomConstruction factory, int maximum) throws IOException {
        if (!values.isArray() || values.size() > maximum) throw new IOException("Invalid axiom edit inventory");
        Set<OWLAxiom> result = new LinkedHashSet<>();
        for (JsonNode value : values)
            if (!result.add(factory.axiom(value))) throw new IOException("Duplicate native axiom edit");
        return result;
    }
    private static Set<OWLAxiom> difference(OWLOntology first, OWLOntology second) {
        Set<OWLAxiom> result = new LinkedHashSet<>(first.getAxioms());
        result.removeAll(second.getAxioms());
        return result;
    }
    /** No native output is authoritative before complete original qualification and reread. */
    public static Map<String,Object> evaluate(JsonNode request, Path job, Map<String,Object> result) throws Exception {
        int cap = WorkerProtocol.positive(request.path("max_bytes"));
        int maximum = WorkerProtocol.positive(request.path("max_nodes"));
        int resources = WorkerProtocol.positive(request.path("max_resources"));
        int issues = WorkerProtocol.positive(request.path("max_issues"));
        int depth = WorkerProtocol.positive(request.path("projection_depth"));
        result.put("qualified", false);
        String action = WorkerProtocol.text(request.path("action"), 32);
        JsonNode sourceRequest = action.equals("compare") ? request.path("before") : request;
        Path sourceDirectory = action.equals("compare") ? job.resolve("before") : job;
        NativeDocuments.Loaded input = NativeDocuments.loadRequest(sourceRequest, sourceDirectory);
        List<Map<String,Object>> losses = new ArrayList<>();
        input.qualifyOriginals(losses, cap, issues);
        result.put("changes", losses);
        if (!losses.isEmpty()) return result;
        OWLOntology original = input.ontology;
        Set<OWLAxiom> additions, removals;
        if (action.equals("compare")) {
            NativeDocuments.Loaded second = NativeDocuments.loadRequest(request.path("after"), job.resolve("after"));
            second.qualifyOriginals(losses, cap, issues);
            if (!losses.isEmpty()) return result;
            if (!original.getOntologyID().equals(second.ontology.getOntologyID())
                    || !original.getAnnotations().equals(second.ontology.getAnnotations())
                    || !original.getImportsDeclarations().equals(second.ontology.getImportsDeclarations())) {
                result.put("header_compatible", false); return result;
            }
            additions = difference(second.ontology, original);
            removals = difference(original, second.ontology);
        } else if (action.equals("apply")) {
            AxiomConstruction factory = new AxiomConstruction(original.getOWLOntologyManager().getOWLDataFactory(), maximum, depth, cap);
            additions = construct(request.path("additions"), factory, maximum);
            removals = construct(request.path("removals"), factory, maximum);
            for (OWLAxiom removal : removals)
                if (!original.containsAxiom(removal)) { result.put("preconditions", false); return result; }
            for (OWLAxiom addition : additions)
                if (original.containsAxiom(addition) || removals.contains(addition)) { result.put("preconditions", false); return result; }
        } else throw new IOException("Unknown axiom transaction action");
        result.put("preconditions", true);
        result.put("header_compatible", true);
        result.put("additions", AxiomProjection.projectChanges(additions, maximum, depth, cap));
        result.put("removals", AxiomProjection.projectChanges(removals, maximum, depth, cap));
        if (action.equals("apply")) {
            OWLOntologyManager manager = original.getOWLOntologyManager();
            manager.removeAxioms(original, removals);
            manager.addAxioms(original, additions);
            result.putAll(NativeDocuments.checkedWrite(input, job, request.path("profiles"), cap, issues, resources));
        } else result.put("qualified", true);
        return result;
    }
}
