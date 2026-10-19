package org.bijux.ontology.datatypes;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.IOException;
import java.nio.file.Path;
import java.util.*;
import org.bijux.ontology.axioms.AxiomConstruction;
import org.bijux.ontology.axioms.AxiomProjection;
import org.bijux.ontology.backend.QualifiedReasoning;
import org.bijux.ontology.backend.WorkerProtocol;
import org.semanticweb.owlapi.model.*;
import org.semanticweb.owlapi.model.parameters.Imports;
import org.semanticweb.owlapi.reasoner.*;

/** Datatype difference/intersection through maintained class satisfiability. */
public final class DatatypeCompatibility {
    private DatatypeCompatibility() {}

    private static OWLDataRange combined(List<OWLDataPropertyRangeAxiom> witnesses,
            OWLDataFactory factory) throws IOException {
        if (witnesses.isEmpty()) throw new IOException("MissingGlobalDataPropertyRange");
        Set<OWLDataRange> ranges = new LinkedHashSet<>();
        for (OWLDataPropertyRangeAxiom witness : witnesses) ranges.add(witness.getRange());
        return ranges.size() == 1 ? ranges.iterator().next() : factory.getOWLDataIntersectionOf(ranges);
    }

    /** Use actual inferred property hierarchy, not a competing graph closure. */
    private static List<OWLDataPropertyRangeAxiom> ranges(QualifiedReasoning input,
            IRI propertyIri, int maximum) throws IOException {
        if (!input.ontology.containsDataPropertyInSignature(propertyIri, Imports.INCLUDED))
            throw new IOException("MissingSelectedDataProperty");
        OWLDataProperty property = input.ontology.getOWLOntologyManager().getOWLDataFactory().getOWLDataProperty(propertyIri);
        Set<OWLDataProperty> properties = new LinkedHashSet<>();
        properties.add(property);
        for (OWLDataProperty equivalent : input.reasoner.getEquivalentDataProperties(property).getEntities()) {
            if (properties.size() >= maximum && !properties.contains(equivalent)) throw new WorkerProtocol.OutputLimit();
            properties.add(equivalent);
        }
        for (OWLDataProperty parent : input.reasoner.getSuperDataProperties(property, false).getFlattened()) {
            if (properties.size() >= maximum && !properties.contains(parent)) throw new WorkerProtocol.OutputLimit();
            properties.add(parent);
        }
        List<OWLDataPropertyRangeAxiom> result = new ArrayList<>();
        for (OWLOntology member : input.ontology.getImportsClosure())
            for (OWLDataProperty selected : properties)
                for (OWLDataPropertyRangeAxiom axiom : member.getDataPropertyRangeAxioms(selected)) {
                    if (result.size() >= maximum) throw new WorkerProtocol.OutputLimit();
                    result.add(axiom);
                }
        return result;
    }

    private static Map<String,Object> projection(OWLDataRange range, OWLDataProperty property,
            OWLDataFactory factory, int maximum, int depth, int cap) throws IOException {
        return AxiomProjection.projectChanges(Collections.singletonList(
            factory.getOWLDataPropertyRangeAxiom(property, range)), maximum, depth, cap);
    }

    /** Original consistency precedes every fresh-property reduction. */
    public static Map<String,Object> evaluate(JsonNode request, Path job,
            Map<String,Object> result) throws Exception {
        int cap = WorkerProtocol.positive(request.path("max_bytes"));
        int maximum = WorkerProtocol.positive(request.path("max_nodes"));
        int depth = WorkerProtocol.positive(request.path("query_depth"));
        List<Map<String,Object>> answers = new ArrayList<>();
        result.put("answers", answers); result.put("temporary_consistent", null);
        result.put("fresh_property", null); result.put("fresh_absent", null);
        try (QualifiedReasoning input = QualifiedReasoning.open(request, job, result)) {
            if (input == null) return result;
            boolean consistent = input.reasoner.isConsistent();
            result.put("consistent", consistent);
            if (!consistent) return result;
            JsonNode queries = request.path("obligations");
            if (!queries.isArray() || queries.size() == 0 || queries.size() > maximum)
                throw new IOException("Invalid required datatype inventory");
            OWLDataFactory factory = input.ontology.getOWLOntologyManager().getOWLDataFactory();
            AxiomConstruction construction = new AxiomConstruction(factory, maximum, depth, cap);
            List<OWLDataRange> actuals = new ArrayList<>(), required = new ArrayList<>();
            List<List<OWLDataPropertyRangeAxiom>> witnesses = new ArrayList<>();
            List<String> errors = new ArrayList<>();
            Set<IRI> queryNames = new HashSet<>();
            for (JsonNode query : queries) {
                OWLDataRange actual = null, expected = null;
                List<OWLDataPropertyRangeAxiom> ranges = Collections.emptyList();
                String error = null;
                try {
                    if (query.isNull()) throw new IOException("UnsupportedHostDatatypeProfile");
                    String kind = WorkerProtocol.text(query.path("kind"), 32);
                    if (!Arrays.asList("inclusion", "disjointness", "field").contains(kind))
                        throw new IOException("UnsupportedDatatypeRelationship");
                    actual = construction.dataRange(query.path("actual"));
                    if (kind.equals("field")) {
                        IRI property = IRI.create(WorkerProtocol.text(query.path("property"), cap));
                        if (!property.isAbsolute()) throw new IOException("InvalidDataPropertyIRI");
                        ranges = ranges(input, property, maximum);
                        expected = combined(ranges, factory);
                    } else expected = construction.dataRange(query.path("required"));
                    for (OWLEntity entity : actual.getSignature()) queryNames.add(entity.getIRI());
                    for (OWLEntity entity : expected.getSignature()) queryNames.add(entity.getIRI());
                    if (queryNames.size() > maximum) throw new WorkerProtocol.OutputLimit();
                } catch (TimeOutException | WorkerProtocol.OutputLimit exhausted) { throw exhausted; }
                catch (Exception unsupported) { error = unsupported.getClass().getSimpleName() + ":" + unsupported.getMessage(); }
                actuals.add(actual); required.add(expected); witnesses.add(ranges); errors.add(error);
            }
            OWLDataProperty fresh = null;
            for (int suffix = 0; suffix < maximum; suffix++) {
                IRI candidate = IRI.create("urn:bijux:ontology:datatype:witness:" + suffix);
                if (!input.ontology.containsEntityInSignature(candidate, Imports.INCLUDED) && !queryNames.contains(candidate)) {
                    fresh = factory.getOWLDataProperty(candidate); break;
                }
            }
            if (fresh == null) throw new WorkerProtocol.OutputLimit();
            result.put("fresh_property", fresh.getIRI().toString()); result.put("fresh_absent", true);
            input.ontology.getOWLOntologyManager().addAxiom(input.ontology, factory.getOWLDeclarationAxiom(fresh));
            input.reasoner.flush();
            boolean temporary = input.reasoner.isConsistent();
            result.put("temporary_consistent", temporary);
            if (!temporary) return result;
            for (int index = 0; index < queries.size(); index++) {
                Map<String,Object> answer = new LinkedHashMap<>();
                answer.put("index", index); answer.put("difference_satisfiable", null);
                answer.put("intersection_satisfiable", null); answer.put("error", errors.get(index));
                answer.put("actual", null); answer.put("required", null); answer.put("ranges", null);
                try {
                    if (errors.get(index) == null) {
                        OWLDataRange actual = actuals.get(index), expected = required.get(index);
                        answer.put("actual", projection(actual, fresh, factory, maximum, depth, cap));
                        answer.put("required", projection(expected, fresh, factory, maximum, depth, cap));
                        answer.put("ranges", AxiomProjection.projectChanges(new ArrayList<OWLAxiom>(witnesses.get(index)), maximum, depth, cap));
                        String kind = queries.get(index).path("kind").textValue();
                        // Capture both native booleans atomically for a field: a
                        // late unsupported/timeout cannot publish half a verdict.
                        Boolean difference = null, intersection = null;
                        if (!kind.equals("disjointness")) difference = input.reasoner.isSatisfiable(
                            factory.getOWLDataSomeValuesFrom(fresh, factory.getOWLDataIntersectionOf(actual, factory.getOWLDataComplementOf(expected))));
                        if (!kind.equals("inclusion")) intersection = input.reasoner.isSatisfiable(
                            factory.getOWLDataSomeValuesFrom(fresh, factory.getOWLDataIntersectionOf(actual, expected)));
                        answer.put("difference_satisfiable", difference); answer.put("intersection_satisfiable", intersection);
                    }
                } catch (TimeOutException | WorkerProtocol.OutputLimit exhausted) { throw exhausted; }
                catch (Exception unsupported) {
                    answer.put("difference_satisfiable", null); answer.put("intersection_satisfiable", null);
                    answer.put("error", unsupported.getClass().getSimpleName());
                }
                answers.add(answer);
            }
        } catch (TimeOutException | WorkerProtocol.OutputLimit exhausted) { result.put("status", "exhausted"); }
        catch (OutOfMemoryError | StackOverflowError exhausted) { result.put("status", "exhausted"); }
        catch (Exception unsupported) { result.put("qualified", false); result.put("error", unsupported.getClass().getSimpleName()); }
        return result;
    }
}
