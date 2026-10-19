package org.bijux.ontology.entailment;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.IOException;
import java.nio.file.Path;
import java.util.*;
import org.bijux.ontology.backend.WorkerProtocol;
import org.bijux.ontology.backend.QualifiedReasoning;
import org.semanticweb.owlapi.model.*;
import org.semanticweb.owlapi.model.parameters.Imports;
import org.semanticweb.owlapi.reasoner.*;

/** Supported typed axiom queries use maintained factories and actual isEntailed. */
public final class EntailmentChecks {
    private EntailmentChecks() {}

    private static final class QueryFactory {
        final OWLDataFactory factory;
        final int cap, maximum, depth;
        int used;
        QueryFactory(OWLDataFactory factory, int cap, int maximum, int depth) {
            this.factory = factory; this.cap = cap; this.maximum = maximum; this.depth = depth;
        }
        private void node(int level) throws IOException {
            if (++used > maximum || level > depth) throw new WorkerProtocol.OutputLimit();
        }
        private IRI iri(JsonNode value) throws IOException {
            String text = WorkerProtocol.text(value, cap);
            IRI iri = IRI.create(text);
            if (!iri.isAbsolute()) throw new IOException("Query IRI is not absolute");
            return iri;
        }
        private IRI entity(JsonNode value, String expected) throws IOException {
            node(1);
            if (!expected.equals(WorkerProtocol.text(value.path("kind"), 32)))
                throw new IOException("Query entity role mismatch");
            return iri(value.path("iri"));
        }
        private OWLClassExpression expression(JsonNode value, int level) throws IOException {
            node(level);
            String kind = WorkerProtocol.text(value.path("kind"), 32);
            switch (kind) {
                case "class": return factory.getOWLClass(iri(value.path("iri")));
                case "complement": return factory.getOWLObjectComplementOf(expression(value.path("operand"), level + 1));
                case "intersection":
                case "union": {
                    JsonNode members = value.path("operands");
                    if (!members.isArray() || members.size() < 2 || members.size() > maximum)
                        throw new IOException("Unsupported query junction");
                    List<OWLClassExpression> values = new ArrayList<>();
                    for (JsonNode member : members) values.add(expression(member, level + 1));
                    return kind.equals("intersection") ? factory.getOWLObjectIntersectionOf(values)
                        : factory.getOWLObjectUnionOf(values);
                }
                case "some":
                case "all": {
                    if (!value.path("inverse").isBoolean()) throw new IOException("Invalid object inverse flag");
                    OWLObjectPropertyExpression property = factory.getOWLObjectProperty(iri(value.path("property")));
                    if (value.path("inverse").booleanValue()) property = property.getInverseProperty();
                    OWLClassExpression filler = expression(value.path("filler"), level + 1);
                    return kind.equals("some") ? factory.getOWLObjectSomeValuesFrom(property, filler)
                        : factory.getOWLObjectAllValuesFrom(property, filler);
                }
                default: throw new IOException("Unsupported query class expression");
            }
        }
        OWLAxiom axiom(JsonNode value) throws IOException {
            node(1);
            switch (WorkerProtocol.text(value.path("kind"), 32)) {
                case "subclass": return factory.getOWLSubClassOfAxiom(expression(value.path("sub"), 1), expression(value.path("super"), 1));
                case "class_assertion": return factory.getOWLClassAssertionAxiom(expression(value.path("expression"), 1), factory.getOWLNamedIndividual(entity(value.path("individual"), "named_individual")));
                default: throw new IOException("Unsupported query axiom type");
            }
        }
    }

    /** Never run an ordinary positive or negative consumer proof under inconsistency. */
    public static Map<String,Object> evaluate(JsonNode request, Path job,
            Map<String,Object> result) throws Exception {
        result.put("answers", new ArrayList<Map<String,Object>>());
        try (QualifiedReasoning input = QualifiedReasoning.open(request, job, result)) {
            if (input == null) return result;
            return evaluatePrepared(request, input, result);
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

    /** Evaluate finite queries on an already qualified, caller-owned unchanged theory. */
    public static Map<String,Object> evaluatePrepared(JsonNode request, QualifiedReasoning input,
            Map<String,Object> result) throws Exception {
        int cap = WorkerProtocol.positive(request.path("max_bytes"));
        int maximum = WorkerProtocol.positive(request.path("max_nodes"));
        List<Map<String,Object>> answers = new ArrayList<>();
        result.put("answers", answers);
        try {
            boolean consistent = input.reasoner.isConsistent();
            result.put("consistent", consistent);
            if (!consistent) return result;
            JsonNode queries = request.path("obligations");
            if (!queries.isArray() || queries.size() == 0 || queries.size() > maximum)
                throw new IOException("Invalid required entailment inventory");
            QueryFactory factory = new QueryFactory(input.ontology.getOWLOntologyManager().getOWLDataFactory(),
                cap, maximum, WorkerProtocol.positive(request.path("query_depth")));
            for (int index = 0; index < queries.size(); index++) {
                if (answers.size() >= maximum) throw new WorkerProtocol.OutputLimit();
                Map<String,Object> answer = new LinkedHashMap<>();
                answer.put("index", index); answer.put("entailed", null); answer.put("error", null);
                try {
                    if (queries.get(index).isNull()) throw new IOException("Unsupported host query profile");
                    OWLAxiom axiom = factory.axiom(queries.get(index));
                    if (!input.reasoner.isEntailmentCheckingSupported(axiom.getAxiomType()))
                        throw new UnsupportedEntailmentTypeException(axiom);
                    for (OWLEntity entity : axiom.getSignature())
                        if (!entity.isBuiltIn() && !input.ontology.containsEntityInSignature(entity, Imports.INCLUDED))
                            throw new IOException("Query entity lacks selected source signature");
                    answer.put("entailed", input.reasoner.isEntailed(axiom));
                } catch (TimeOutException | WorkerProtocol.OutputLimit exhausted) {
                    throw exhausted;
                } catch (Exception unsupported) {
                    answer.put("error", unsupported.getClass().getSimpleName());
                }
                answers.add(answer);
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
