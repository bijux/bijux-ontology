package org.bijux.ontology.documents;

import org.bijux.ontology.backend.WorkerProtocol;
import static org.bijux.ontology.backend.WorkerProtocol.*;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import javax.xml.XMLConstants;
import javax.xml.parsers.*;
import javax.xml.transform.sax.SAXSource;
import javax.xml.transform.stream.StreamSource;
import javax.xml.validation.SchemaFactory;
import org.obolibrary.obo2owl.OWLAPIOwl2Obo;
import org.obolibrary.oboformat.model.OBODoc;
import org.obolibrary.oboformat.model.Frame;
import org.obolibrary.oboformat.model.Clause;
import org.obolibrary.oboformat.parser.OBOFormatParser;
import org.obolibrary.oboformat.writer.OBOFormatWriter;
import org.semanticweb.owlapi.apibinding.OWLManager;
import org.semanticweb.owlapi.formats.*;
import org.semanticweb.owlapi.io.*;
import org.semanticweb.owlapi.formats.TurtleDocumentFormat;
import org.semanticweb.owlapi.model.*;
import org.semanticweb.owlapi.profiles.OWLProfileReport;
import org.semanticweb.owlapi.profiles.OWLProfileViolation;
import org.w3c.dom.ls.DOMImplementationLS;
import org.w3c.dom.ls.LSInput;
import org.xml.sax.InputSource;
import org.xml.sax.XMLReader;
import org.xml.sax.Attributes;
import org.xml.sax.SAXException;
import org.xml.sax.helpers.XMLFilterImpl;
import org.semanticweb.owlapi.profiles.OWL2DLProfile;
import org.semanticweb.owlapi.profiles.violations.UseOfNonAbsoluteIRI;

/** Original native documents, offline maintained parsing and checked writer output. */
public final class NativeDocuments {
    private NativeDocuments() {}

    private static OWLDocumentFormat format(String name) throws IOException {
        switch (name) {
            case "owlxml": return new OWLXMLDocumentFormat();
            case "functional": return new FunctionalSyntaxDocumentFormat();
            case "manchester": return new ManchesterSyntaxDocumentFormat();
            case "obo": return new OBODocumentFormat();
            default: throw new IOException("Unsupported native format");
        }
    }

    private static final class Document {
        final Path path;
        final String effective;
        final String syntax;
        Document(Path path, String effective, String syntax) {
            this.path = path; this.effective = effective; this.syntax = syntax;
        }
    }

    /** Only captured streams enter the maintained factory; no URL reader is available. */
    private static final class OwnedFactory implements OWLOntologyFactory {
        private final OWLOntologyFactory delegate;
        private final Map<String,Document> documents;
        OwnedFactory(OWLOntologyFactory delegate, Map<String,Document> documents) {
            this.delegate = delegate; this.documents = documents;
        }
        @Override public boolean canCreateFromDocumentIRI(IRI iri) {
            return delegate.canCreateFromDocumentIRI(iri);
        }
        @Override public boolean canAttemptLoading(OWLOntologyDocumentSource source) {
            return documents.containsKey(source.getDocumentIRI().toString());
        }
        @Override public OWLOntology createOWLOntology(OWLOntologyManager manager,
                OWLOntologyID id, IRI iri, OWLOntologyCreationHandler handler)
                throws OWLOntologyCreationException {
            return delegate.createOWLOntology(manager, id, iri, handler);
        }
        @Override public OWLOntology loadOWLOntology(OWLOntologyManager manager,
                OWLOntologyDocumentSource source, OWLOntologyCreationHandler handler,
                OWLOntologyLoaderConfiguration configuration) throws OWLOntologyCreationException {
            Document document = documents.get(source.getDocumentIRI().toString());
            if (document == null) throw new OWLOntologyCreationException("Unowned native import");
            try {
                if (document.syntax.equals("owlxml")) validateXml(document);
                try (InputStream input = Files.newInputStream(document.path)) {
                    return delegate.loadOWLOntology(manager,
                        new StreamDocumentSource(input, IRI.create(document.effective),
                            format(document.syntax), null), handler, configuration);
                }
            } catch (Exception error) {
                throw new OWLOntologyCreationException(error);
            }
        }
    }

    /** Official pinned schemas are local, with external DTD/schema retrieval forbidden. */
    private static void validateXml(Document document) throws Exception {
        String prefix = "/org/bijux/ontology/documents/";
        SchemaFactory schemas = SchemaFactory.newInstance(XMLConstants.W3C_XML_SCHEMA_NS_URI);
        schemas.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true);
        schemas.setProperty(XMLConstants.ACCESS_EXTERNAL_DTD, "");
        schemas.setProperty(XMLConstants.ACCESS_EXTERNAL_SCHEMA, "");
        DOMImplementationLS dom = (DOMImplementationLS) DocumentBuilderFactory.newInstance()
            .newDocumentBuilder().getDOMImplementation().getFeature("LS", "3.0");
        schemas.setResourceResolver((type, namespace, publicId, systemId, base) -> {
            if (!"http://www.w3.org/XML/1998/namespace".equals(namespace)
                    || !"http://www.w3.org/2001/xml.xsd".equals(systemId))
                throw new IllegalArgumentException("Unowned schema import");
            LSInput input = dom.createLSInput();
            input.setSystemId(systemId);
            input.setByteStream(NativeDocuments.class.getResourceAsStream(prefix + "xml.xsd"));
            return input;
        });
        try (InputStream schema = NativeDocuments.class.getResourceAsStream(prefix + "owl2-xml.xsd")) {
            if (schema == null) throw new IOException("Pinned schema unavailable");
            javax.xml.validation.Validator validator = schemas.newSchema(new StreamSource(schema)).newValidator();
            validator.setProperty(XMLConstants.ACCESS_EXTERNAL_DTD, "");
            validator.setProperty(XMLConstants.ACCESS_EXTERNAL_SCHEMA, "");
            SAXParserFactory factory = SAXParserFactory.newInstance();
            factory.setNamespaceAware(true);
            factory.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true);
            factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
            factory.setFeature("http://xml.org/sax/features/external-general-entities", false);
            factory.setFeature("http://xml.org/sax/features/external-parameter-entities", false);
            XMLReader reader = new IriQualification(factory.newSAXParser().getXMLReader(), document.effective, Files.size(document.path));
            try (InputStream input = Files.newInputStream(document.path)) {
                InputSource source = new InputSource(input);
                source.setSystemId(document.effective);
                validator.validate(new SAXSource(reader, source));
            }
        }
    }

    /** A finite input qualification on maintained SAX events, never an IRI rewrite. */
    private static final class IriQualification extends XMLFilterImpl {
        private boolean explicitBase;
        private boolean fragmentSeen;
        private String iriElement;
        private StringBuilder iriText;
        private final String effective;
        private final long maximum;
        IriQualification(XMLReader reader, String effective, long maximum) {
            super(reader); this.effective = effective; this.maximum = maximum;
        }
        private void absolute(String value) throws SAXException {
            if (!IRI.create(value).isAbsolute()) throw new SAXException("Relative IRI unsupported");
        }
        private void qualifiedIri(String value, boolean allowFragment) throws SAXException {
            boolean fragment = allowFragment && value.startsWith("#") && !explicitBase
                && effective.indexOf('#') < 0 && !java.net.URI.create(effective).isOpaque();
            if (!fragment) absolute(value);
            else fragmentSeen = true;
        }
        @Override public void startElement(String uri, String local, String name, Attributes attributes)
                throws SAXException {
            String base = attributes.getValue(XMLConstants.XML_NS_URI, "base");
            if (base != null) {
                absolute(base);
                if (fragmentSeen) throw new SAXException("Fragment with xml:base unsupported");
                explicitBase = true;
            }
            if ("http://www.w3.org/2002/07/owl#".equals(uri)) {
                // OWL/XML permits exactly one of the full and abbreviated IRI forms.
                if (attributes.getValue("", "IRI") != null
                        && attributes.getValue("", "abbreviatedIRI") != null)
                    throw new SAXException("Ambiguous full/abbreviated IRI");
                for (String field : Arrays.asList("IRI", "ontologyIRI", "versionIRI", "datatypeIRI")) {
                    String value = attributes.getValue("", field);
                    if (value == null) continue;
                    qualifiedIri(value, field.equals("IRI") && !local.equals("Prefix"));
                }
                if (local.equals("Import") || local.equals("IRI")) {
                    iriElement = local; iriText = new StringBuilder();
                }
            }
            super.startElement(uri, local, name, attributes);
        }
        @Override public void characters(char[] chars, int start, int length) throws SAXException {
            if (iriElement != null) {
                if (length > maximum - iriText.length())
                    throw new SAXException("IRI text bound", new WorkerProtocol.OutputLimit());
                iriText.append(chars, start, length);
            }
            super.characters(chars, start, length);
        }
        @Override public void endElement(String uri, String local, String name) throws SAXException {
            if (iriElement != null && "http://www.w3.org/2002/07/owl#".equals(uri) && local.equals(iriElement)) {
                qualifiedIri(iriText.toString(), local.equals("IRI"));
                iriElement = null; iriText = null;
            }
            super.endElement(uri, local, name);
        }
    }

    private static OWLOntology load(List<Document> documents, Map<String,Integer> aliases,
            int root, int maximum) throws Exception {
        OWLOntologyManager manager = OWLManager.createOWLOntologyManager();
        Map<String,Document> owned = new HashMap<>();
        for (Document document : documents) {
            if (owned.put(document.effective, document) != null)
                throw new IOException("Ambiguous effective document identity");
        }
        OWLOntologyFactory delegate = manager.getOntologyFactories().iterator().next();
        manager.setOntologyFactories(Collections.singleton(new OwnedFactory(delegate, owned)));
        manager.getIRIMappers().clear();
        manager.getIRIMappers().add(iri -> {
            Integer index = aliases.get(iri.toString());
            return index == null ? iri : IRI.create(documents.get(index).effective);
        });
        OWLOntologyLoaderConfiguration configuration = new OWLOntologyLoaderConfiguration()
            .clearIgnoredImports().setStrict(true).setRepairIllegalPunnings(false).setEntityExpansionLimit("1000")
            .setMissingImportHandlingStrategy(MissingImportHandlingStrategy.THROW_EXCEPTION)
            .setRetriesToAttempt(1).setConnectionTimeout(1).setFollowRedirects(false);
        OWLOntology ontology = manager.loadOntologyFromOntologyDocument(
            new IRIDocumentSource(IRI.create(documents.get(root).effective)), configuration);
        if (manager.getOntologies().size() > maximum) throw new WorkerProtocol.OutputLimit();
        for (OWLProfileViolation violation : new OWL2DLProfile().checkOntology(ontology).getViolations())
            if (violation instanceof UseOfNonAbsoluteIRI)
                throw new IOException("Parsed relative IRI unsupported");
        for (OWLOntology member : manager.getOntologies()) {
            if (member.getOntologyID().getOntologyIRI().filter(iri -> !iri.isAbsolute()).isPresent()
                    || member.getOntologyID().getVersionIRI().filter(iri -> !iri.isAbsolute()).isPresent())
                throw new IOException("Relative ontology identity unsupported");
            for (OWLImportsDeclaration declaration : member.getImportsDeclarations())
                if (!declaration.getIRI().isAbsolute()) throw new IOException("Relative import unsupported");
            Document owner = owned.get(manager.getOntologyDocumentIRI(member).toString());
            if (owner == null || !format(owner.syntax).getKey().equals(manager.getOntologyFormat(member).getKey()))
                throw new IOException("Native parser used an unselected format");
        }
        return ontology;
    }

    /** One operation's actual owned parse; no public live-JVM Python proxy. */
    public static final class Loaded {
        public final OWLOntology ontology;
        public final Map<String,Integer> owners;
        public final List<Map<String,Object>> loaders;
        private final List<Document> documents;
        private final Map<String,Integer> aliases;
        private Loaded(OWLOntology ontology, List<Document> documents,
                Map<String,Integer> aliases, Map<String,Integer> owners,
                List<Map<String,Object>> loaders) {
            this.ontology = ontology; this.documents = documents; this.aliases = aliases;
            this.owners = Collections.unmodifiableMap(owners);
            this.loaders = Collections.unmodifiableList(loaders);
        }

        /** Preserve the native format bridge's original-clause qualification. */
        public void qualifyOriginals(List<Map<String,Object>> changes, int cap, int maximum)
                throws Exception {
            if (documents == null) return;
            OWLOntology first = ontology;
            for (OWLOntology member : first.getOWLOntologyManager().getOntologies()) {
                if (member.anonymousIndividuals().findAny().isPresent())
                    throw new IOException("Anonymous individual structural correspondence unavailable");
                String iri = member.getOWLOntologyManager().getOntologyDocumentIRI(member).toString();
                for (Document document : documents)
                    if (document.effective.equals(iri)) qualifyObo(document, member, changes, cap, maximum);
            }

        }

        /** Actual selected ontology headers, separate from original RDF metadata. */
        public List<Map<String,Object>> records(int resources) throws Exception {
            OWLOntology first = ontology;
            List<Map<String,Object>> loaded = new ArrayList<>();
            Set<String> seen = new HashSet<>();
            for (OWLOntology member : first.getOWLOntologyManager().getOntologies()) {
                String effective = first.getOWLOntologyManager().getOntologyDocumentIRI(member).toString();
                Integer index = owners.get(effective);
                if (index == null || !seen.add(effective)) throw new IOException("Ambiguous loaded document");
                Map<String,Object> record = new LinkedHashMap<>();
                record.put("document", index);
                record.put("ontology_iri", member.getOntologyID().getOntologyIRI().map(Object::toString).orElse(null));
                record.put("version_iri", member.getOntologyID().getVersionIRI().map(Object::toString).orElse(null));
                record.put("axioms", member.getAxiomCount());
                List<String> imports = new ArrayList<>();
                for (OWLImportsDeclaration declaration : member.getImportsDeclarations()) {
                    if (imports.size() >= resources) throw new WorkerProtocol.OutputLimit();
                    imports.add(declaration.getIRI().toString());
                }
                record.put("imports", imports);
                loaded.add(record);
            }
            return loaded;
        }
    }

    /** The existing staged RDF parser policy and its actual loader metadata. */
    public static Loaded loadRdfRequest(JsonNode request, Path job) throws Exception {
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
        return new Loaded(ontology, null, null, owners, loaders);
    }

    /** Capture and load only supplied original native streams. */
    public static Loaded loadRequest(JsonNode request, Path job) throws Exception {
        int cap = WorkerProtocol.positive(request.path("max_bytes"));
        int resources = WorkerProtocol.positive(request.path("max_resources"));
        JsonNode inputs = request.path("documents");
        if (!inputs.isArray() || inputs.size() == 0 || inputs.size() > resources)
            throw new IOException("Invalid native document inventory");
        List<Document> documents = new ArrayList<>();
        for (JsonNode item : inputs) {
            Path path = Path.of(WorkerProtocol.text(item.path("path"), cap)).toAbsolutePath().normalize();
            if (!job.equals(path.getParent()) || Files.isSymbolicLink(path) || !Files.isRegularFile(path)
                    || Files.size(path) > cap) throw new IOException("Unowned native document path");
            String syntax = WorkerProtocol.text(item.path("format"), 32);
            format(syntax);
            String effective = WorkerProtocol.text(item.path("effective_url"), cap);
            documents.add(new Document(path, effective, syntax));
        }
        JsonNode mapping = request.path("aliases");
        if (!mapping.isObject() || mapping.size() > resources * 4) throw new IOException("Invalid aliases");
        Map<String,Integer> aliases = new HashMap<>();
        Iterator<Map.Entry<String,JsonNode>> entries = mapping.fields();
        while (entries.hasNext()) {
            Map.Entry<String,JsonNode> entry = entries.next();
            if (entry.getKey().length() > cap || !entry.getValue().isInt()) throw new IOException("Invalid alias");
            int index = entry.getValue().intValue();
            if (index < 0 || index >= documents.size()) throw new IOException("Unowned alias");
            aliases.put(entry.getKey(), index);
        }
        OWLOntology first = load(documents, aliases, 0, resources);
        Map<String,Integer> owners = new HashMap<>();
        for (int index = 0; index < documents.size(); index++)
            owners.put(documents.get(index).effective, index);
        return new Loaded(first, documents, aliases, owners, Collections.emptyList());
    }

    private static final class CappedOutput extends FilterOutputStream {
        private final long cap;
        private long used;
        private boolean exhausted;
        CappedOutput(OutputStream output, long cap) { super(output); this.cap = cap; }
        @Override public void write(int value) throws IOException {
            if (used >= cap) { exhausted = true; throw new WorkerProtocol.OutputLimit(); }
            used++; out.write(value);
        }
        @Override public void write(byte[] bytes, int start, int length) throws IOException {
            if (length > cap - used) { exhausted = true; throw new WorkerProtocol.OutputLimit(); }
            used += length; out.write(bytes, start, length);
        }
        @Override public void close() throws IOException {
            // Native text writers may swallow write failures; the owned close must retain exhaustion.
            try { super.close(); }
            finally { if (exhausted) throw new WorkerProtocol.OutputLimit(); }
        }
    }

    /** Write an import-free structural result and independently reread its exact axioms. */
    public static OWLOntology checkedWriteIsolated(OWLOntology original, Path output,
            int cap, int maximum) throws Exception {
        if (!original.getImportsDeclarations().isEmpty()
                || original.anonymousIndividuals().findAny().isPresent())
            throw new IOException("Isolated native output requires no imports or anonymous individuals");
        try (OutputStream stream = new CappedOutput(
                Files.newOutputStream(output, StandardOpenOption.CREATE_NEW), cap)) {
            original.getOWLOntologyManager().saveOntology(original, format("functional"), stream);
        }
        String effective = "urn:bijux:ontology:structural-result";
        OWLOntology reread = load(Collections.singletonList(new Document(output, effective, "functional")),
                Collections.singletonMap(effective, 0), 0, 1);
        List<Map<String,Object>> changes = new ArrayList<>();
        compare(original, reread, changes, cap, maximum);
        if (!changes.isEmpty()) throw new IOException("Native result changed during independent reread");
        return reread;
    }

    private static final class ChangeLimit extends IOException {}

    private static void loss(List<Map<String,Object>> changes, String kind, Object value,
            int cap, int maximum) throws IOException {
        if (changes.size() >= maximum) throw new ChangeLimit();
        Map<String,Object> change = new LinkedHashMap<>();
        change.put("kind", kind);
        change.put("value", WorkerProtocol.rendering(value, cap));
        changes.add(change);
    }

    private static void compare(OWLOntology first, OWLOntology second,
            List<Map<String,Object>> changes, int cap, int maximum) throws IOException {
        if (!first.getOntologyID().getOntologyIRI().equals(second.getOntologyID().getOntologyIRI())
                || !first.getOntologyID().getVersionIRI().equals(second.getOntologyID().getVersionIRI()))
            loss(changes, "ontology_identity", second.getOntologyID(), cap, maximum);
        for (OWLAxiom axiom : first.getAxioms())
            if (!second.containsAxiom(axiom)) loss(changes, "removed_axiom", axiom, cap, maximum);
        for (OWLAxiom axiom : second.getAxioms())
            if (!first.containsAxiom(axiom)) loss(changes, "added_axiom", axiom, cap, maximum);
        for (OWLAnnotation annotation : first.getAnnotations())
            if (!second.getAnnotations().contains(annotation)) loss(changes, "removed_annotation", annotation, cap, maximum);
        for (OWLAnnotation annotation : second.getAnnotations())
            if (!first.getAnnotations().contains(annotation)) loss(changes, "added_annotation", annotation, cap, maximum);
        if (!first.getImportsDeclarations().equals(second.getImportsDeclarations()))
            loss(changes, "import_declarations", second.getImportsDeclarations(), cap, maximum);
    }

    private static void oboFrame(Frame original, Frame restored,
            List<Map<String,Object>> changes, int cap, int maximum) throws IOException {
        List<Clause> remaining = restored == null ? new ArrayList<>()
            : new ArrayList<>(restored.getClauses());
        for (Clause clause : original.getClauses()) {
            if (!remaining.remove(clause))
                loss(changes, "unrepresented_obo_clause", original.getId() + " " + clause, cap, maximum);
        }
    }

    /** Native OBO clauses must survive the actual OBO-to-OWL bridge too. */
    private static void qualifyObo(Document document, OWLOntology ontology,
            List<Map<String,Object>> changes, int cap, int maximum) throws Exception {
        if (!document.syntax.equals("obo")) return;
        OBOFormatParser parser = new OBOFormatParser();
        parser.setFollowImports(false);
        OBODoc original;
        try (Reader reader = Files.newBufferedReader(document.path, StandardCharsets.UTF_8)) {
            original = parser.parse(reader);
        }
        OWLAPIOwl2Obo converter = new OWLAPIOwl2Obo(ontology.getOWLOntologyManager());
        converter.setDiscardUntranslatable(true);
        OBODoc restored = converter.convert(ontology);
        oboFrame(original.getHeaderFrame(), restored.getHeaderFrame(), changes, cap, maximum);
        for (Frame frame : original.getTermFrames())
            oboFrame(frame, restored.getTermFrame(frame.getId()), changes, cap, maximum);
        for (Frame frame : original.getTypedefFrames())
            oboFrame(frame, restored.getTypedefFrame(frame.getId()), changes, cap, maximum);
        for (Frame frame : original.getInstanceFrames())
            oboFrame(frame, restored.getInstanceFrame(frame.getId()), changes, cap, maximum);
    }

    private static List<Map<String,Object>> profiles(OWLOntology ontology, JsonNode selected,
            int cap, int maximum) throws IOException {
        if (!selected.isArray() || selected.size() > 4) throw new IOException("Invalid profile selection");
        List<Map<String,Object>> records = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (JsonNode item : selected) {
            String name = WorkerProtocol.text(item, 32);
            if (!seen.add(name)) throw new IOException("Duplicate profile selection");
            OWLProfileReport report = WorkerProtocol.profile(name).checkOntology(ontology);
            Map<String,Object> record = new LinkedHashMap<>();
            record.put("profile", name);
            record.put("in_profile", report.isInProfile());
            List<String> violations = new ArrayList<>();
            for (OWLProfileViolation violation : report.getViolations()) {
                if (violations.size() >= maximum) throw new WorkerProtocol.OutputLimit();
                violations.add(WorkerProtocol.rendering(violation, cap));
            }
            record.put("violations", violations);
            records.add(record);
        }
        return records;
    }

    /** Serialize an owned edited root and recheck its exact annotated native structure. */
    public static Map<String,Object> checkedWrite(Loaded input, Path job, JsonNode selectedProfiles,
            int cap, int maximum, int resources) throws Exception {
        if (input.documents == null) throw new IOException("Native original documents required");
        Path output = job.resolve("candidate.owl");
        try (OutputStream stream = new CappedOutput(Files.newOutputStream(output, StandardOpenOption.CREATE_NEW), cap)) {
            input.ontology.getOWLOntologyManager().saveOntology(input.ontology, format("functional"), stream);
        }
        List<Document> updated = new ArrayList<>(input.documents);
        updated.set(0, new Document(output, input.documents.get(0).effective, "functional"));
        OWLOntology restored = load(updated, input.aliases, 0, resources);
        List<Map<String,Object>> changes = new ArrayList<>();
        compare(input.ontology, restored, changes, cap, maximum);
        if (restored.anonymousIndividuals().findAny().isPresent())
            throw new IOException("Anonymous individual structural correspondence unavailable");
        Map<String,Object> result = new LinkedHashMap<>();
        result.put("changes", changes);
        result.put("after_profiles", profiles(restored, selectedProfiles, cap, maximum));
        result.put("bytes", Files.size(output));
        result.put("qualified", changes.isEmpty());
        return result;
    }

    /** Serialize selected owned modules together and reread the exact original alias closure. */
    public static Map<String,Object> checkedWriteModules(Loaded input, Path job, Set<Integer> owned,
            JsonNode selectedProfiles, int cap, int maximum, int resources) throws Exception {
        if (input.documents == null) throw new IOException("Native original documents required");
        OWLOntologyManager manager = input.ontology.getOWLOntologyManager();
        Map<Integer,OWLOntology> originals = new TreeMap<>();
        for (OWLOntology member : manager.getOntologies()) {
            Integer index = input.owners.get(manager.getOntologyDocumentIRI(member).toString());
            if (index == null || originals.put(index, member) != null)
                throw new IOException("Ambiguous original module owner");
        }
        if (!originals.keySet().containsAll(owned)) throw new IOException("Unloaded selected module");
        List<Document> updated = new ArrayList<>(input.documents);
        long written = 0;
        Map<Integer,Long> sizes = new TreeMap<>();
        for (Map.Entry<Integer,OWLOntology> entry : originals.entrySet()) {
            int index = entry.getKey();
            if (!owned.contains(index)) continue;
            Path output = job.resolve("candidate-" + index + ".owl");
            try (OutputStream stream = new CappedOutput(Files.newOutputStream(output, StandardOpenOption.CREATE_NEW), cap - written)) {
                manager.saveOntology(entry.getValue(), format("functional"), stream);
            }
            long size = Files.size(output); written += size; sizes.put(index, size);
            updated.set(index, new Document(output, input.documents.get(index).effective, "functional"));
        }
        OWLOntology restoredRoot = load(updated, input.aliases, 0, resources);
        Map<Integer,OWLOntology> restored = new TreeMap<>();
        for (OWLOntology member : restoredRoot.getOWLOntologyManager().getOntologies()) {
            String effective = restoredRoot.getOWLOntologyManager().getOntologyDocumentIRI(member).toString();
            Integer index = input.owners.get(effective);
            if (index == null || restored.put(index, member) != null)
                throw new IOException("Ambiguous reread module owner");
            if (member.anonymousIndividuals().findAny().isPresent())
                throw new IOException("Anonymous individual structural correspondence unavailable");
        }
        if (!originals.keySet().equals(restored.keySet())) throw new IOException("Reread module scope changed");
        List<Map<String,Object>> changes = new ArrayList<>();
        List<Map<String,Object>> modules = new ArrayList<>();
        for (Map.Entry<Integer,OWLOntology> entry : originals.entrySet()) {
            int index = entry.getKey();
            compare(entry.getValue(), restored.get(index), changes, cap, maximum);
            if (owned.contains(index)) {
                Map<String,Object> module = new LinkedHashMap<>();
                module.put("document", index); module.put("bytes", sizes.get(index));
                module.put("after_profiles", profiles(restored.get(index), selectedProfiles, cap, maximum));
                modules.add(module);
            }
        }
        Map<String,Object> result = new LinkedHashMap<>();
        result.put("changes", changes); result.put("modules", modules);
        result.put("qualified", changes.isEmpty());
        return result;
    }

    public static Map<String,Object> evaluate(JsonNode request, Path job, Map<String,Object> result) throws Exception {
        List<Map<String,Object>> changes = new ArrayList<>();
        String syntax = WorkerProtocol.text(request.path("format"), 32);
        result.put("format", syntax);
        result.put("native_loader_profile", "original-native-strict-offline-schema-qualified");
        result.put("changes", changes);
        result.put("qualified", false);
        try {
            return convert(request, job, result, changes);
        } catch (ChangeLimit | WorkerProtocol.OutputLimit exhausted) {
            result.put("status", "exhausted");
        } catch (Exception unavailable) {
            Throwable cause = unavailable;
            while (cause.getCause() != null && cause.getCause() != cause) cause = cause.getCause();
            if (cause instanceof WorkerProtocol.OutputLimit) result.put("status", "exhausted");
        }
        return result;
    }

    private static Map<String,Object> convert(JsonNode request, Path job, Map<String,Object> result,
            List<Map<String,Object>> changes) throws Exception {
        int cap = WorkerProtocol.positive(request.path("max_bytes"));
        int maximum = WorkerProtocol.positive(request.path("max_issues"));
        int resources = WorkerProtocol.positive(request.path("max_resources"));
        String outputFormat = WorkerProtocol.text(request.path("format"), 32);
        format(outputFormat);
        Loaded input = loadRequest(request, job);
        List<Document> documents = input.documents;
        Map<String,Integer> aliases = input.aliases;
        OWLOntology first = input.ontology;
        input.qualifyOriginals(changes, cap, maximum);
        Path output = job.resolve("converted.owl");
        try (OutputStream stream = new CappedOutput(Files.newOutputStream(output, StandardOpenOption.CREATE_NEW), cap)) {
            if (outputFormat.equals("obo")) {
                OWLAPIOwl2Obo converter = new OWLAPIOwl2Obo(first.getOWLOntologyManager());
                converter.setDiscardUntranslatable(true);
                OBODoc converted = converter.convert(first);
                for (OWLAxiom axiom : converter.getUntranslatableAxioms())
                    loss(changes, "untranslatable_axiom", axiom, cap, maximum);
                Writer writer = new OutputStreamWriter(stream, StandardCharsets.UTF_8);
                new OBOFormatWriter().write(converted, writer);
                writer.flush();
            } else first.getOWLOntologyManager().saveOntology(first, format(outputFormat), stream);
        }
        List<Document> updated = new ArrayList<>(documents);
        updated.set(0, new Document(output, documents.get(0).effective, outputFormat));
        OWLOntology second = load(updated, aliases, 0, resources);
        if (second.anonymousIndividuals().findAny().isPresent())
            throw new IOException("Anonymous individual structural correspondence unavailable");
        compare(first, second, changes, cap, maximum);
        result.put("documents", input.records(resources));
        result.put("changes", changes);
        result.put("before_profiles", profiles(first, request.path("profiles"), cap, maximum));
        result.put("after_profiles", profiles(second, request.path("profiles"), cap, maximum));
        result.put("format", outputFormat);
        result.put("bytes", Files.size(output));
        result.put("qualified", true);
        return result;
    }
}
