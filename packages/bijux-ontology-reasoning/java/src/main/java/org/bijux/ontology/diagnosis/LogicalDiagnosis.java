package org.bijux.ontology.diagnosis;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.IOException;
import java.nio.file.Path;
import java.util.*;
import org.bijux.ontology.backend.WorkerProtocol;
import org.bijux.ontology.backend.QualifiedReasoning;
import org.semanticweb.owlapi.model.*;
import org.semanticweb.owlapi.model.parameters.Imports;
import org.semanticweb.owlapi.reasoner.*;

/** Actual mature DL queries over the same owned and qualified input closure. */
public final class LogicalDiagnosis {
    private LogicalDiagnosis() {}

    /** Bound inventories, retain completed native answers, and never infer from absence. */
    public static Map<String,Object> evaluate(JsonNode request, Path job,
            Map<String,Object> result) throws Exception {
        int cap = WorkerProtocol.positive(request.path("max_bytes"));
        int maximum = WorkerProtocol.positive(request.path("max_nodes"));
        List<Map<String,Object>> classes = new ArrayList<>();
        List<String> selected = new ArrayList<>();
        result.put("classes", classes); result.put("selected", selected);
        try (QualifiedReasoning input = QualifiedReasoning.open(request, job, result)) {
            if (input == null) return result;
            OWLOntology ontology = input.ontology;
            OWLReasoner reasoner = input.reasoner;
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
        }
        return result;
    }
}
