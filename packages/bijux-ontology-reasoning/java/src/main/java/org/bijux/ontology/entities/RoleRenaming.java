package org.bijux.ontology.entities;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.IOException;
import java.nio.file.Path;
import java.util.*;
import org.bijux.ontology.backend.WorkerProtocol;
import org.bijux.ontology.documents.NativeDocuments;
import org.semanticweb.owlapi.model.*;

/** One finite closure-wide structural rename, with no partial ownership acceptance. */
public final class RoleRenaming {
    private RoleRenaming() {}

    private static OWLEntity entity(OWLDataFactory factory, String role, IRI iri) throws IOException {
        switch (role) {
            case "class": return factory.getOWLClass(iri);
            case "object_property": return factory.getOWLObjectProperty(iri);
            case "data_property": return factory.getOWLDataProperty(iri);
            case "annotation_property": return factory.getOWLAnnotationProperty(iri);
            case "datatype": return factory.getOWLDatatype(iri);
            case "named_individual": return factory.getOWLNamedIndividual(iri);
            default: throw new IOException("Unsupported entity role");
        }
    }

    private static void rendering(List<String> values, Object value, int maximum, int cap) throws IOException {
        if (values.size() >= maximum) throw new WorkerProtocol.OutputLimit();
        values.add(WorkerProtocol.rendering(value, cap));
    }

    /** All original modules are examined before any manager receives changes. */
    public static Map<String,Object> evaluate(JsonNode request, Path job, Map<String,Object> result) throws Exception {
        int cap = WorkerProtocol.positive(request.path("max_bytes"));
        int maximum = WorkerProtocol.positive(request.path("max_nodes"));
        int resources = WorkerProtocol.positive(request.path("max_resources"));
        int issues = WorkerProtocol.positive(request.path("max_issues"));
        result.put("qualified", false);
        List<Map<String,Object>> losses = new ArrayList<>();
        result.put("changes", losses);
        NativeDocuments.Loaded input = NativeDocuments.loadRequest(request, job);
        input.qualifyOriginals(losses, cap, issues);
        if (!losses.isEmpty()) return result;
        OWLOntologyManager manager = input.ontology.getOWLOntologyManager();
        OWLDataFactory factory = manager.getOWLDataFactory();
        IRI old = IRI.create(WorkerProtocol.text(request.path("source"), cap));
        IRI next = IRI.create(WorkerProtocol.text(request.path("target"), cap));
        if (old.equals(next)) throw new IOException("A rename must change identity");
        Set<Integer> owned = new LinkedHashSet<>();
        if (!request.path("owned").isArray() || request.path("owned").size() > resources)
            throw new IOException("Invalid module ownership");
        for (JsonNode item : request.path("owned")) {
            if (!item.isIntegralNumber() || !item.canConvertToInt() || item.intValue() < 0
                    || !owned.add(item.intValue())) throw new IOException("Invalid owned module index");
        }
        if (owned.isEmpty()) throw new IOException("No owned modules selected");
        List<String> selected = new ArrayList<>();
        if (!request.path("roles").isArray() || request.path("roles").isEmpty()
                || request.path("roles").size() > 6) throw new IOException("Invalid entity roles");
        for (JsonNode item : request.path("roles")) selected.add(WorkerProtocol.text(item, 32));
        if (selected.equals(List.of("all"))) selected = List.of("class", "object_property", "data_property", "annotation_property", "datatype", "named_individual");
        if (new HashSet<>(selected).size() != selected.size()) throw new IOException("Duplicate roles");
        Map<OWLEntity,IRI> roles = new HashMap<>();
        boolean found = false;
        Set<Integer> loaded = new HashSet<>();
        for (OWLOntology ontology : manager.getOntologies()) {
            Integer index = input.owners.get(manager.getOntologyDocumentIRI(ontology).toString());
            if (index == null || !loaded.add(index)) throw new IOException("Unowned native document");
            if (ontology.getSignature().size() > maximum) throw new WorkerProtocol.OutputLimit();
            if (ontology.getOntologyID().getOntologyIRI().filter(next::equals).isPresent()
                    || ontology.getOntologyID().getVersionIRI().filter(next::equals).isPresent()
                    || ontology.containsEntityInSignature(next)) {
                result.put("failure", "target_collision"); return result;
            }
            for (String role : selected) {
                OWLEntity value = entity(factory, role, old);
                if (value.isBuiltIn()) { result.put("failure", "builtin_source"); return result; }
                if (entity(factory, role, next).isBuiltIn()) { result.put("failure", "target_collision"); return result; }
                roles.put(value, next);
                if (ontology.containsEntityInSignature(value)) found = true;
            }
        }
        if (loaded.size() != request.path("documents").size() || !loaded.containsAll(owned)) {
            result.put("failure", "module_scope"); return result;
        }
        if (!found) { result.put("failure", "source_role_absent"); return result; }
        AnnotationRoles duplicator = new AnnotationRoles(roles, manager, old, next,
                request.path("annotations"), maximum, cap);
        List<OWLOntologyChange> planned = new ArrayList<>();
        List<Map<String,Object>> edits = new ArrayList<>();
        int visited = 0;
        try {
            for (OWLOntology ontology : manager.getOntologies()) {
                int index = input.owners.get(manager.getOntologyDocumentIRI(ontology).toString());
                List<String> additions = new ArrayList<>(), removals = new ArrayList<>();
                for (OWLAxiom original : ontology.getAxioms()) {
                    if (++visited > maximum) throw new WorkerProtocol.OutputLimit();
                    // Rules are outside this operation's complete supported profile.
                    if (original instanceof SWRLRule) {
                        result.put("failure", "unsupported_axiom"); return result;
                    }
                    OWLAxiom changed = duplicator.duplicateObject(original);
                    if (!original.equals(changed)) {
                        if (!owned.contains(index)) { result.put("failure", "unowned_module"); return result; }
                        if (ontology.containsAxiom(changed)) { result.put("failure", "axiom_collision"); return result; }
                        planned.add(new RemoveAxiom(ontology, original)); planned.add(new AddAxiom(ontology, changed));
                        rendering(removals, original, maximum, cap); rendering(additions, changed, maximum, cap);
                    }
                }
                for (OWLAnnotation original : ontology.getAnnotations()) {
                    if (++visited > maximum) throw new WorkerProtocol.OutputLimit();
                    OWLAnnotation changed = duplicator.duplicateObject(original);
                    if (!original.equals(changed)) {
                        if (!owned.contains(index)) { result.put("failure", "unowned_module"); return result; }
                        if (ontology.getAnnotations().contains(changed)) { result.put("failure", "annotation_collision"); return result; }
                        planned.add(new RemoveOntologyAnnotation(ontology, original)); planned.add(new AddOntologyAnnotation(ontology, changed));
                        rendering(removals, original, maximum, cap); rendering(additions, changed, maximum, cap);
                    }
                }
                if (owned.contains(index)) {
                    Map<String,Object> edit = new LinkedHashMap<>();
                    edit.put("document", index); edit.put("additions", additions); edit.put("removals", removals);
                    edits.add(edit);
                }
            }
        } catch (AnnotationRoles.MissingPolicy missing) {
            result.put("failure", "annotation_policy");
            result.put("failure_value", WorkerProtocol.rendering(missing.getMessage(), cap));
            return result;
        }
        manager.applyChanges(planned);
        result.put("edits", edits);
        result.putAll(NativeDocuments.checkedWriteModules(input, job, owned, request.path("profiles"), cap, issues, resources));
        return result;
    }
}
