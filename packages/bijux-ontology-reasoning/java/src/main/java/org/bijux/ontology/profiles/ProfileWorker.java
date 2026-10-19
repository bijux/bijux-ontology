package org.bijux.ontology.profiles;

import static org.bijux.ontology.backend.WorkerProtocol.*;

import org.bijux.ontology.documents.NativeDocuments;
import org.bijux.ontology.axioms.AxiomProjection;
import org.bijux.ontology.diagnosis.LogicalDiagnosis;
import org.bijux.ontology.entailment.EntailmentChecks;
import com.fasterxml.jackson.core.StreamReadConstraints;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.*;
import java.nio.file.*;
import java.util.*;
import org.semanticweb.owlapi.model.*;
import org.semanticweb.owlapi.profiles.*;

/** A bounded structured boundary around actual OWLAPI profile implementations. */
public final class ProfileWorker {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final byte[] EXHAUSTED = "{\"protocol\":1,\"status\":\"exhausted\"}".getBytes(java.nio.charset.StandardCharsets.UTF_8);
    private static final byte[] FAILED = "{\"protocol\":1,\"status\":\"unavailable\"}".getBytes(java.nio.charset.StandardCharsets.UTF_8);

    private ProfileWorker() {}

    /** Bound structured result storage separately from diagnostic streams. */
    private static final class BoundedOutput extends FilterOutputStream {
        private long written;
        private final long limit;
        BoundedOutput(OutputStream stream, long limit) { super(stream); this.limit = limit; }
        @Override public void write(int value) throws IOException {
            if (++written > limit) throw new OutputLimit();
            out.write(value);
        }
        @Override public void write(byte[] value, int start, int length) throws IOException {
            if (length > limit - written) throw new OutputLimit();
            written += length;
            out.write(value, start, length);
        }
    }

    private static Map<String,Object> evaluate(JsonNode request, Path job) throws Exception {
        Map<String,Object> result = identity(request);
        String mode = text(request.path("operation"), 32);
        if (mode.equals("capability")) return result;
        if (mode.equals("convert")) return NativeDocuments.evaluate(request, job, result);
        if (mode.equals("role_rename")) return org.bijux.ontology.entities.RoleRenaming.evaluate(request, job, result);
        if (mode.equals("axiom_changes")) return org.bijux.ontology.axioms.AxiomChanges.evaluate(request, job, result);
        if (mode.equals("axioms")) return AxiomProjection.evaluate(request, job, result);
        if (mode.equals("diagnosis")) return LogicalDiagnosis.evaluate(request, job, result);
        if (mode.equals("model_compatibility")) return org.bijux.ontology.models.ModelCompatibility.evaluate(request, job, result);
        if (mode.equals("datatypes")) return org.bijux.ontology.datatypes.DatatypeCompatibility.evaluate(request, job, result);
        if (mode.equals("relationships")) return org.bijux.ontology.relationships.InferredRelationships.evaluate(request, job, result);
        if (mode.equals("entailment")) return EntailmentChecks.evaluate(request, job, result);
        if (!mode.equals("profile")) throw new IOException("Unsupported operation");
        int cap = positive(request.path("max_bytes"));
        int maximum = positive(request.path("max_issues"));
        NativeDocuments.Loaded input = NativeDocuments.loadRdfRequest(request, job);
        OWLOntology ontology = input.ontology;
        OWLOntologyManager manager = ontology.getOWLOntologyManager();
        Map<String,Integer> owners = input.owners;
        List<Map<String,Object>> loaders = input.loaders;
        String selected = text(request.path("profile"), 32);
        OWLProfileReport checked = profile(selected).checkOntology(ontology);
        result.put("profile", selected);
        result.put("raw_in_profile", checked.isInProfile());
        result.put("loaders", loaders);
        List<Map<String,Object>> violations = new ArrayList<>();
        for (OWLProfileViolation violation : checked.getViolations()) {
            if (violations.size() >= maximum) {
                result.put("status", "exhausted");
                break;
            }
            Integer owner = owners.get(manager.getOntologyDocumentIRI(violation.getOntology()).toString());
            if (owner == null) throw new IOException("Unowned violation ontology");
            Map<String,Object> item = new LinkedHashMap<>();
            item.put("kind", violation.getClass().getSimpleName());
            item.put("document", owner);
            item.put("axiom", rendering(violation.getAxiom(), cap));
            item.put("expression", rendering(violation.getExpression(), cap));
            violations.add(item);
        }
        result.put("violations", violations);
        return result;
    }

    public static void main(String[] arguments) {
        if (arguments.length != 3) return;
        Path input = Path.of(arguments[0]).toAbsolutePath().normalize();
        Path output = Path.of(arguments[1]).toAbsolutePath().normalize();
        int maximum;
        try { maximum = Integer.parseInt(arguments[2]); }
        catch (NumberFormatException error) { return; }
        if (maximum < EXHAUSTED.length || !Objects.equals(input.getParent(), output.getParent())) return;
        try {
            if (Files.size(input) > maximum) throw new OutputLimit();
            JSON.getFactory().setStreamReadConstraints(StreamReadConstraints.builder().maxNestingDepth(128).maxStringLength(maximum).maxNumberLength(128).build());
            JsonNode request = JSON.readTree(input.toFile());
            Map<String,Object> result = evaluate(request, input.getParent());
            try (OutputStream stream = new BoundedOutput(Files.newOutputStream(output, StandardOpenOption.CREATE_NEW), maximum)) {
                JSON.writeValue(stream, result);
            }
        } catch (OutOfMemoryError | StackOverflowError | OutputLimit exhausted) {
            try { Files.write(output, EXHAUSTED); } catch (IOException ignored) { }
        } catch (Exception failed) {
            Throwable cause = failed;
            while (cause.getCause() != null && cause.getCause() != cause) cause = cause.getCause();
            try { Files.write(output, cause instanceof OutputLimit ? EXHAUSTED : FAILED); } catch (IOException ignored) { }
        }
    }
}
