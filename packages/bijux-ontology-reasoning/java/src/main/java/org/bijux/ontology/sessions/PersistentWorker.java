package org.bijux.ontology.sessions;

import com.fasterxml.jackson.core.StreamReadConstraints;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.*;
import org.bijux.ontology.axioms.AxiomConstruction;
import org.bijux.ontology.backend.QualifiedReasoning;
import org.bijux.ontology.backend.WorkerProtocol;
import org.bijux.ontology.entailment.EntailmentChecks;
import org.bijux.ontology.relationships.InferredRelationships;
import org.semanticweb.owlapi.apibinding.OWLManager;
import org.semanticweb.owlapi.formats.TurtleDocumentFormat;
import org.semanticweb.owlapi.io.StreamDocumentSource;
import org.semanticweb.owlapi.model.*;
import org.semanticweb.owlapi.reasoner.InferenceType;

/** One explicitly captured, read-only classified theory and finite independent jobs. */
public final class PersistentWorker {
    private static final ObjectMapper JSON = new ObjectMapper();
    private PersistentWorker() {}

    /** Retain only protocol bytes already proved to fit the actual frame cap. */
    private static final class CappedBytes extends ByteArrayOutputStream {
        private final int maximum;
        CappedBytes(int maximum) { this.maximum = maximum; }
        @Override public synchronized void write(int value) {
            if (count >= maximum) throw new FrameLimit();
            super.write(value);
        }
        @Override public synchronized void write(byte[] value, int start, int length) {
            if (length > maximum - count) throw new FrameLimit();
            super.write(value, start, length);
        }
    }
    private static final class FrameLimit extends RuntimeException {}

    private static JsonNode read(DataInputStream stream, int maximum) throws IOException {
        long size;
        try { size = stream.readLong(); } catch (EOFException ended) { return null; }
        if (size < 1 || size > maximum) throw new IOException("Invalid session frame size");
        byte[] data = new byte[(int) size];
        stream.readFully(data);
        JsonNode result = JSON.readTree(data);
        if (!result.isObject()) throw new IOException("Session requests require an object");
        return result;
    }

    private static void write(DataOutputStream stream, Map<String,Object> value,
            int maximum) throws IOException {
        CappedBytes bytes = new CappedBytes(maximum);
        JSON.writeValue(bytes, value);
        stream.writeLong(bytes.size());
        bytes.writeTo(stream);
        stream.flush();
    }

    private static Map<String,Object> failed(String status) {
        Map<String,Object> result = new LinkedHashMap<>();
        result.put("protocol", 1); result.put("status", status);
        return result;
    }

    /** Build a separate ontology from selected rechecked subclass consequences. */
    private static void projection(JsonNode request, QualifiedReasoning theory,
            Map<String,Object> result, int maximum) throws Exception {
        result.put("projection_complete", false);
        if (!theory.reasoner.isConsistent()) return;
        int nodes = WorkerProtocol.positive(request.path("max_nodes"));
        JsonNode values = request.path("axioms");
        if (!values.isArray() || values.size() > nodes)
            throw new IOException("Invalid selected projection inventory");
        OWLOntologyManager manager = OWLManager.createOWLOntologyManager();
        OWLDataFactory factory = manager.getOWLDataFactory();
        OWLOntology output = manager.createOntology();
        AxiomConstruction constructor = new AxiomConstruction(factory, nodes,
            WorkerProtocol.positive(request.path("query_depth")), maximum);
        for (JsonNode value : values) {
            OWLAxiom axiom = constructor.axiom(value);
            if (!(axiom instanceof OWLSubClassOfAxiom)
                    || !theory.reasoner.isEntailed(axiom))
                throw new IOException("Projection contains an unproved subclass consequence");
            manager.addAxiom(output, axiom);
            for (OWLEntity entity : axiom.getSignature())
                if (!entity.isBuiltIn()) manager.addAxiom(output, factory.getOWLDeclarationAxiom(entity));
        }
        CappedBytes encoded = new CappedBytes(maximum);
        manager.saveOntology(output, new TurtleDocumentFormat(), encoded);
        byte[] bytes = encoded.toByteArray();
        OWLOntologyManager reader = OWLManager.createOWLOntologyManager();
        OWLOntology restored = reader.loadOntologyFromOntologyDocument(new StreamDocumentSource(
            new ByteArrayInputStream(bytes), IRI.create("urn:bijux:ontology:inference"),
            new TurtleDocumentFormat(), null));
        if (!output.getAxioms().equals(restored.getAxioms())
                || !restored.getImportsDeclarations().isEmpty()
                || !restored.getIndividualsInSignature().isEmpty())
            throw new IOException("Inference serialization changed the selected ontology");
        result.put("content", new String(bytes, StandardCharsets.UTF_8));
        result.put("projection_complete", true);
    }

    public static void main(String[] arguments) {
        if (arguments.length != 2) return;
        int maximum;
        try { maximum = Integer.parseInt(arguments[1]); }
        catch (NumberFormatException malformed) { return; }
        if (maximum < 128) return;
        Path directory = Path.of(arguments[0]).toAbsolutePath().normalize();
        DataInputStream incoming = new DataInputStream(System.in);
        DataOutputStream outgoing = new DataOutputStream(System.out);
        // Native diagnostics cannot become protocol frames.
        System.setOut(new PrintStream(System.err, true));
        JSON.getFactory().setStreamReadConstraints(StreamReadConstraints.builder()
            .maxNestingDepth(128).maxStringLength(maximum).maxNumberLength(128).build());
        QualifiedReasoning theory = null;
        try {
            JsonNode first = read(incoming, maximum);
            if (first == null || !"prepare".equals(WorkerProtocol.text(first.path("operation"), 32)))
                throw new IOException("Expected captured theory preparation");
            String identity = WorkerProtocol.text(first.path("theory_identity"), 64);
            Map<String,Object> prepared = WorkerProtocol.identity(first);
            prepared.put("theory_identity", identity);
            prepared.put("process_id", ProcessHandle.current().pid());
            theory = QualifiedReasoning.open(first, directory, prepared);
            int classified = 0;
            if (theory != null) {
                boolean consistent = theory.reasoner.isConsistent();
                prepared.put("consistent", consistent);
                if (consistent) {
                    theory.reasoner.precomputeInferences(InferenceType.CLASS_HIERARCHY);
                    classified = 1;
                    if (!theory.reasoner.isPrecomputed(InferenceType.CLASS_HIERARCHY))
                        throw new IOException("Native classification was not retained");
                }
            }
            prepared.put("loads", theory == null ? 0 : 1);
            prepared.put("classifications", classified);
            write(outgoing, prepared, maximum);
            if (theory == null) return;
            while (true) {
                JsonNode job = read(incoming, maximum);
                if (job == null) return;
                WorkerProtocol.identity(job);
                if (!identity.equals(WorkerProtocol.text(job.path("theory_identity"), 64))
                        || !first.path("manifest_sha256").equals(job.path("manifest_sha256")))
                    throw new IOException("Session job changed its fixed theory identity");
                int cap = Math.min(maximum, WorkerProtocol.positive(job.path("max_bytes")));
                ObjectNode request = ((ObjectNode) first).deepCopy();
                for (String key : Arrays.asList("operation", "obligations", "queries", "axioms",
                        "max_nodes", "max_results", "max_issues", "max_bytes", "query_depth",
                        "task_timeout_ms"))
                    if (job.has(key)) request.set(key, job.get(key));
                Map<String,Object> result = new LinkedHashMap<>(prepared);
                String operation = WorkerProtocol.text(request.path("operation"), 32);
                if (operation.equals("entailment"))
                    EntailmentChecks.evaluatePrepared(request, theory, result);
                else if (operation.equals("relationships"))
                    InferredRelationships.evaluatePrepared(request, theory, result);
                else if (operation.equals("projection")) projection(request, theory, result, cap);
                else throw new IOException("Unsupported session operation");
                write(outgoing, result, cap);
                if (!"completed".equals(result.get("status"))
                        || !Boolean.TRUE.equals(result.get("qualified"))) return;
            }
        } catch (OutOfMemoryError | StackOverflowError | FrameLimit exhausted) {
            try { write(outgoing, failed("exhausted"), maximum); }
            catch (Exception ignored) { }
        } catch (Exception failure) {
            try { write(outgoing, failed("unavailable"), maximum); }
            catch (Exception ignored) { }
        } finally {
            if (theory != null) theory.close();
        }
    }
}
