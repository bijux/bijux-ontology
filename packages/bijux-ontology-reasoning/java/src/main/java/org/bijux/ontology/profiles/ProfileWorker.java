package org.bijux.ontology.profiles;

import com.fasterxml.jackson.core.StreamReadConstraints;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.*;
import java.nio.file.*;
import java.util.*;
import org.semanticweb.owlapi.apibinding.OWLManager;
import org.semanticweb.owlapi.formats.TurtleDocumentFormat;
import org.semanticweb.owlapi.io.OWLOntologyLoaderMetaData;
import org.semanticweb.owlapi.io.StreamDocumentSource;
import org.semanticweb.owlapi.model.*;
import org.semanticweb.owlapi.profiles.*;
import org.semanticweb.owlapi.util.VersionInfo;

/** A bounded structured boundary around actual OWLAPI profile implementations. */
public final class ProfileWorker {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final byte[] EXHAUSTED = "{\"protocol\":1,\"status\":\"exhausted\"}".getBytes(java.nio.charset.StandardCharsets.UTF_8);
    private static final byte[] FAILED = "{\"protocol\":1,\"status\":\"unavailable\"}".getBytes(java.nio.charset.StandardCharsets.UTF_8);
    private static final List<String> PROFILES = Arrays.asList("owl2-dl", "owl2-el", "owl2-ql", "owl2-rl");

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

    private static final class OutputLimit extends IOException {}

    private static String text(JsonNode node, int maximum) throws IOException {
        if (!node.isTextual() || node.textValue().length() > maximum) throw new IOException("Invalid text field");
        return node.textValue();
    }

    private static int positive(JsonNode node) throws IOException {
        if (!node.isIntegralNumber() || !node.canConvertToInt() || node.intValue() <= 0) throw new IOException("Invalid positive bound");
        return node.intValue();
    }

    private static String rendering(Object value, int maximum) throws IOException {
        if (value == null) return null;
        String result = value.toString();
        if (result.length() > maximum) throw new OutputLimit();
        return result;
    }

    private static OWLProfile profile(String selected) throws IOException {
        switch (selected) {
            case "owl2-dl": return new OWL2DLProfile();
            case "owl2-el": return new OWL2ELProfile();
            case "owl2-ql": return new OWL2QLProfile();
            case "owl2-rl": return new OWL2RLProfile();
            default: throw new IOException("Unsupported profile");
        }
    }

    private static Map<String,Object> identity(JsonNode request) throws IOException {
        if (!request.path("protocol").isInt() || request.path("protocol").intValue() != 1) throw new IOException("Protocol mismatch");
        String version = VersionInfo.getVersionInfo().getVersion();
        if (!version.equals("5.5.1") || Runtime.version().feature() < 11) throw new IOException("Backend version mismatch");
        Map<String,Object> result = new LinkedHashMap<>();
        result.put("protocol", 1);
        result.put("status", "completed");
        result.put("manifest_sha256", text(request.path("manifest_sha256"), 64));
        result.put("owlapi", version);
        result.put("loader_profile", "turtle-permissive-no-punning-repair-offline");
        result.put("java", System.getProperty("java.version"));
        result.put("java_major", Runtime.version().feature());
        result.put("vm", System.getProperty("java.vm.name"));
        result.put("profiles", PROFILES);
        return result;
    }

    private static Map<String,Object> evaluate(JsonNode request, Path job) throws Exception {
        Map<String,Object> result = identity(request);
        String mode = text(request.path("operation"), 32);
        if (mode.equals("capability")) return result;
        if (!mode.equals("profile")) throw new IOException("Unsupported operation");
        int cap = positive(request.path("max_bytes"));
        int maximum = positive(request.path("max_issues"));
        int resourceCap = positive(request.path("max_resources"));
        JsonNode documents = request.path("documents");
        if (!documents.isArray() || documents.size() == 0 || documents.size() > resourceCap) throw new IOException("Invalid document inventory");
        List<Path> paths = new ArrayList<>();
        List<String> identifiers = new ArrayList<>();
        Map<String,Integer> owners = new HashMap<>();
        for (JsonNode document : documents) {
            String id = text(document.path("id"), cap);
            Path path = Path.of(text(document.path("path"), cap)).toAbsolutePath().normalize();
            if (!path.getParent().equals(job) || Files.isSymbolicLink(path) || !Files.isRegularFile(path) || Files.size(path) > cap) throw new IOException("Unowned document path");
            if (identifiers.contains(id) || owners.put(path.toUri().toString(), paths.size()) != null) throw new IOException("Duplicate document identity");
            identifiers.add(id);
            paths.add(path);
        }
        Map<String,Integer> aliases = new HashMap<>();
        JsonNode supplied = request.path("aliases");
        if (!supplied.isObject() || supplied.size() > resourceCap * 4) throw new IOException("Invalid import map");
        Iterator<Map.Entry<String,JsonNode>> fields = supplied.fields();
        while (fields.hasNext()) {
            Map.Entry<String,JsonNode> field = fields.next();
            if (field.getKey().length() > cap || !field.getValue().isInt()) throw new IOException("Invalid import alias");
            int index = field.getValue().intValue();
            if (index < 0 || index >= paths.size()) throw new IOException("Unowned import target");
            aliases.put(field.getKey(), index);
        }
        int root = request.path("root").asInt(-1);
        if (!request.path("root").isInt() || root < 0 || root >= paths.size()) throw new IOException("Unowned root");
        OWLOntologyManager manager = OWLManager.createOWLOntologyManager();
        Path absent = job.resolve("unmapped-import");
        manager.getIRIMappers().clear();
        manager.getIRIMappers().add(iri -> IRI.create(aliases.containsKey(iri.toString()) ? paths.get(aliases.get(iri.toString())).toUri() : absent.toUri()));
        OWLOntologyLoaderConfiguration configuration = new OWLOntologyLoaderConfiguration()
            .setStrict(false).setRepairIllegalPunnings(false)
            .setMissingImportHandlingStrategy(MissingImportHandlingStrategy.THROW_EXCEPTION)
            .setRetriesToAttempt(1).setConnectionTimeout(1).setFollowRedirects(false);
        OWLOntology ontology;
        try (InputStream stream = Files.newInputStream(paths.get(root))) {
            ontology = manager.loadOntologyFromOntologyDocument(new StreamDocumentSource(stream, IRI.create(paths.get(root).toUri()), new TurtleDocumentFormat(), null), configuration);
        }
        List<Map<String,Object>> loaders = new ArrayList<>();
        Set<Integer> observed = new HashSet<>();
        for (OWLOntology member : manager.getOntologies()) {
            Integer owner = owners.get(manager.getOntologyDocumentIRI(member).toString());
            if (owner == null || !observed.add(owner)) throw new IOException("Unowned or duplicate loaded document");
            Map<String,Object> item = new LinkedHashMap<>();
            item.put("document", owner);
            OWLDocumentFormat format = manager.getOntologyFormat(member);
            Optional<OWLOntologyLoaderMetaData> metadata = format == null ? Optional.empty() : format.getOntologyLoaderMetaData();
            item.put("present", metadata.isPresent());
            if (metadata.isPresent()) {
                OWLOntologyLoaderMetaData data = metadata.get();
                item.put("unparsed", data.getUnparsedTriples().limit((long) maximum + 1).count());
                item.put("guessed", data.getGuessedDeclarations().size());
                item.put("triples", data.getTripleCount());
                item.put("header", data.getHeaderState().toString());
            }
            loaders.add(item);
        }
        if (observed.size() != paths.size()) throw new IOException("Required document was not loaded");
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
