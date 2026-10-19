package org.bijux.ontology.entities;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.IOException;
import java.util.*;
import java.util.stream.Collectors;
import org.bijux.ontology.backend.WorkerProtocol;
import org.semanticweb.owlapi.model.*;
import org.semanticweb.owlapi.util.OWLObjectDuplicator;

/** Preserve untyped IRIs unless their actual annotation position is declared. */
public final class AnnotationRoles extends OWLObjectDuplicator {
    private final OWLDataFactory factory;
    private final IRI original, replacement;
    private final Map<String,Boolean> policies = new HashMap<>();

    /** Missing interpretation is not a license to rewrite an untyped reference. */
    public static final class MissingPolicy extends RuntimeException {
        MissingPolicy(String value) { super(value); }
    }

    public AnnotationRoles(Map<OWLEntity,IRI> roles, OWLOntologyManager manager,
            IRI original, IRI replacement, JsonNode selected, int maximum, int cap) throws IOException {
        super(roles, manager);
        this.factory = manager.getOWLDataFactory();
        this.original = original; this.replacement = replacement;
        if (!selected.isArray() || selected.size() > maximum) throw new IOException("Invalid annotation policies");
        for (JsonNode item : selected) {
            String property = WorkerProtocol.text(item.path("property"), cap);
            String position = WorkerProtocol.text(item.path("position"), 16);
            String action = WorkerProtocol.text(item.path("action"), 16);
            if (!Set.of("subject", "value", "domain", "range").contains(position)
                    || !Set.of("preserve", "rename").contains(action)
                    || policies.put(property + "\n" + position, action.equals("rename")) != null)
                throw new IOException("Invalid annotation policy");
        }
    }

    private IRI position(OWLAnnotationProperty property, String position, IRI iri) {
        if (!iri.equals(original)) return iri;
        Boolean rename = policies.get(property.getIRI().toString() + "\n" + position);
        if (rename == null) throw new MissingPolicy(property.getIRI() + " " + position);
        return rename ? replacement : iri;
    }

    /** Ordinary untyped IRIs, including rule variables, have no selected entity role. */
    @Override public IRI visit(IRI iri) { return iri; }

    @Override public OWLAnnotation visit(OWLAnnotation annotation) {
        OWLAnnotationValue value = annotation.getValue();
        if (value instanceof IRI) value = position(annotation.getProperty(), "value", (IRI) value);
        else value = duplicateObject(value);
        return factory.getOWLAnnotation(duplicateObject(annotation.getProperty()), value,
                annotation.annotations().map(this::duplicateObject));
    }

    @Override public OWLAnnotationAssertionAxiom visit(OWLAnnotationAssertionAxiom axiom) {
        OWLAnnotationSubject subject = axiom.getSubject();
        if (subject instanceof IRI) subject = position(axiom.getProperty(), "subject", (IRI) subject);
        else subject = duplicateObject(subject);
        return factory.getOWLAnnotationAssertionAxiom(duplicateObject(axiom.getProperty()), subject,
                duplicateObject(axiom.getAnnotation()).getValue(),
                axiom.annotations().map(this::duplicateObject).collect(Collectors.toList()));
    }

    @Override public OWLAnnotationPropertyDomainAxiom visit(OWLAnnotationPropertyDomainAxiom axiom) {
        return factory.getOWLAnnotationPropertyDomainAxiom(duplicateObject(axiom.getProperty()),
                position(axiom.getProperty(), "domain", axiom.getDomain()),
                axiom.annotations().map(this::duplicateObject).collect(Collectors.toList()));
    }

    @Override public OWLAnnotationPropertyRangeAxiom visit(OWLAnnotationPropertyRangeAxiom axiom) {
        return factory.getOWLAnnotationPropertyRangeAxiom(duplicateObject(axiom.getProperty()),
                position(axiom.getProperty(), "range", axiom.getRange()),
                axiom.annotations().map(this::duplicateObject).collect(Collectors.toList()));
    }
}
