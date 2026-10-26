package org.bijux.ontology.jena;

import com.google.gson.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import org.apache.jena.graph.*;
import org.apache.jena.datatypes.*;
import org.apache.jena.query.Query;
import org.apache.jena.rdf.model.*;
import org.apache.jena.riot.*;
import org.apache.jena.riot.lang.LabelToNode;
import org.apache.jena.shacl.*;
import org.apache.jena.shacl.engine.ValidationContext;
import org.apache.jena.shacl.engine.constraint.SparqlConstraint;
import org.apache.jena.shacl.parser.*;
import org.apache.jena.shacl.validation.ValidationProc;
import org.apache.jena.sparql.algebra.*;
import org.apache.jena.sparql.algebra.op.*;
import org.apache.jena.sparql.algebra.walker.Walker;
import org.apache.jena.sparql.core.*;
import org.apache.jena.sparql.expr.*;
import org.apache.jena.sparql.function.FunctionRegistry;
import org.apache.jena.sparql.pfunction.PropertyFunctionRegistry;

/** Native parsed shape/target/constraint ownership over supplied memory graphs. */
public final class ShaclValidation {
    private ShaclValidation() {}
    static final String SH = "http://www.w3.org/ns/shacl#";
    static final String RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#";
    static final String ROLE = "urn:bijux:shacl:";
    static final String COVERAGE = "urn:bijux:shacl:coverage:";
    static Node iri(String value) { return NodeFactory.createURI(value); }
    static final Set<String> UNSUPPORTED = Set.of(
        "rule", "js", "jsFunctionName", "jsLibrary", "jsLibraryURL", "expression",
        "target", "ask", "entailment", "values", "filterShape");
    static final Set<String> CASTS = Set.of(
        "http://www.w3.org/2001/XMLSchema#string",
        "http://www.w3.org/2001/XMLSchema#integer",
        "http://www.w3.org/2001/XMLSchema#decimal",
        "http://www.w3.org/2001/XMLSchema#double",
        "http://www.w3.org/2001/XMLSchema#float",
        "http://www.w3.org/2001/XMLSchema#boolean",
        "http://www.w3.org/2001/XMLSchema#dateTime");

    static List<Node> objects(Graph graph, Node node, Node predicate, JenaWorker.Control control) {
        List<Node> result = new ArrayList<>();
        var iterator = graph.find(node, predicate, Node.ANY);
        try {
            while (iterator.hasNext()) { control.work(); control.state(); result.add(iterator.next().getObject()); }
        } finally { iterator.close(); }
        return result;
    }
    static Set<Node> bindings(DatasetGraph dataset, String name, JenaWorker.Control control) {
        Set<Node> result=new LinkedHashSet<>();
        for (Node node:objects(dataset.getGraph(iri(ROLE+"bindings")),
                iri("urn:bijux:shacl:selection:scope"), iri("urn:bijux:shacl:selection:"+name), control)) {
            control.state();result.add(node);
        }
        return result;
    }
    static String serialize(Model model, int cap, JenaWorker.Control control) {
        if (model.size()>control.maximumTriples-control.triples)
            throw new JenaWorker.Stopped("Jena RDF result exceeds its triple reservation");
        for (long index=0;index<model.size();index++) control.triple();
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (JenaWorker.CappedOutput output = new JenaWorker.CappedOutput(bytes, cap)) {
            RDFDataMgr.write(output, model, Lang.NTRIPLES);
        } catch (IOException error) { throw new UncheckedIOException(error); }
        return bytes.toString(StandardCharsets.UTF_8);
    }
    static Map<Node,Set<Node>> parameters(JenaWorker.Control control) {
        Graph vocabulary = ModelFactory.createDefaultModel().getGraph();
        try (InputStream stream = ShaclValidation.class.getResourceAsStream("/std/shacl.ttl")) {
            if (stream == null) throw new JenaWorker.Unavailable("Installed Jena SHACL vocabulary is unavailable");
            RDFParser.source(stream).lang(Lang.TURTLE).parse(vocabulary);
        } catch (IOException error) { throw new UncheckedIOException(error); }
        Map<Node,Set<Node>> result = new HashMap<>();
        var declarations = vocabulary.find(Node.ANY,iri(SH+"parameter"),Node.ANY);
        try {
            while (declarations.hasNext()) {
                control.work(); Triple triple = declarations.next();
                for (Node path:objects(vocabulary,triple.getObject(),iri(SH+"path"),control)) {
                    control.state(); result.computeIfAbsent(path,key->new HashSet<>()).add(triple.getSubject());
                }
            }
        } finally { declarations.close(); }
        return result;
    }
    static Set<Node> declaredParameters(Graph graph, JenaWorker.Control control) {
        Set<Node> result = new HashSet<>();
        var components = graph.find(Node.ANY,iri(RDF+"type"),iri(SH+"ConstraintComponent"));
        try {
            while (components.hasNext()) {
                control.work(); Node component=components.next().getSubject();
                for (Node parameter:objects(graph,component,iri(SH+"parameter"),control))
                    for (Node path:objects(graph,parameter,iri(SH+"path"),control)) {
                        control.state();result.add(path);
                    }
            }
        } finally { components.close(); }
        return result;
    }
    static void query(Query query, JenaWorker.Control control) {
        if (!query.isSelectType() || query.hasDatasetDescription())
            throw new JenaWorker.Unavailable("Only local trusted SELECT constraints are supported");
        OpVisitorBase operations = new OpVisitorBase() {
            @Override public void visit(OpService value) {
                throw new JenaWorker.Unavailable("SHACL SERVICE retrieval is unsupported");
            }
            @Override public void visit(OpPropFunc value) {
                throw new JenaWorker.Unavailable("SHACL property functions are outside this profile");
            }
            @Override public void visit(OpProcedure value) {
                throw new JenaWorker.Unavailable("SHACL procedures are outside this profile");
            }
            @Override public void visit(OpBGP value) {
                for (Triple triple:value.getPattern()) {
                    control.work(); Node predicate=triple.getPredicate();
                    if (predicate.isURI() && PropertyFunctionRegistry.get().isRegistered(predicate.getURI()))
                        throw new JenaWorker.Unavailable("SHACL property functions are outside this profile");
                }
            }
        };
        ExprVisitorBase expressions = new ExprVisitorBase() {
            void check(ExprFunction expression) {
                control.work();String name=expression.getFunctionIRI();
                if (name!=null && (!CASTS.contains(name) || !FunctionRegistry.get().isRegistered(name)))
                    throw new JenaWorker.Unavailable("SHACL function has no selected supported implementation: "+name);
            }
            @Override public void visit(ExprFunction0 value) { check(value); }
            @Override public void visit(ExprFunction1 value) { check(value); }
            @Override public void visit(ExprFunction2 value) { check(value); }
            @Override public void visit(ExprFunction3 value) { check(value); }
            @Override public void visit(ExprFunctionN value) { check(value); }
            @Override public void visit(ExprFunctionOp value) { check(value); }
            @Override public void visit(ExprAggregator value) {
                if (value.getAggregator().getExprList()!=null)
                    Walker.walk(value.getAggregator().getExprList(),operations,this);
            }
        };
        Walker.walk(Algebra.compile(query),operations,expressions);
    }
    static Set<Node> components(Shape shape, Graph graph, Map<Node,Set<Node>> parameters,
                               Set<Node> custom, boolean trusted, JenaWorker.Control control) {
        Set<Node> implemented = new HashSet<>();
        if (shape.deactivated()) return implemented;
        for (Node datatype:objects(graph,shape.getShapeNode(),iri(SH+"datatype"),control)) {
            RDFDatatype validator=datatype.isURI()?TypeMapper.getInstance().getTypeByName(datatype.getURI()):null;
            if (validator==null || validator.getClass()==BaseDatatype.class)
                throw new JenaWorker.Unavailable("Selected datatype has no actual native lexical validator: "+datatype);
        }
        for (Constraint constraint:shape.getConstraints()) {
            control.work();control.state();Node component=constraint.getComponent();
            boolean standard=false;
            for (Set<Node> declarations:parameters.values()) {
                control.work();if (declarations.contains(component)) { standard=true;break; }
            }
            if (!standard) throw new JenaWorker.Unavailable("Selected constraint component is outside the Jena profile: "+component);
            if (constraint instanceof SparqlConstraint sparql) {
                if (!trusted) throw new JenaWorker.Unavailable("SHACL-SPARQL requires trusted_sparql=True");
                query(sparql.getQuery(),control);
            } else if (component.equals(iri(SH+"SPARQLConstraintComponent"))) {
                throw new JenaWorker.Unavailable("Custom SPARQL components are outside this Jena profile");
            }
            implemented.add(component);
        }
        if (!shape.getPropertyShapes().isEmpty()) {
            control.work();control.state();
            implemented.add(iri(SH+"PropertyConstraintComponent"));
        }
        var statements=graph.find(shape.getShapeNode(),Node.ANY,Node.ANY);
        try {
            while (statements.hasNext()) {
                control.work();Triple triple=statements.next();Node predicate=triple.getPredicate();
                if (custom.contains(predicate))
                    throw new JenaWorker.Unavailable("Selected authored custom component is outside this Jena profile");
                if (predicate.isURI() && predicate.getURI().startsWith(SH)) {
                    String name=predicate.getURI().substring(SH.length());
                    if (UNSUPPORTED.contains(name))
                        throw new JenaWorker.Unavailable("Selected SHACL feature is outside this Jena profile: "+name);
                    Set<Node> expected=parameters.get(predicate);
                    if (expected!=null && Collections.disjoint(expected,implemented))
                        throw new JenaWorker.Unavailable("Selected parameter has no actual native constraint implementation: "+name);
                }
            }
        } finally { statements.close(); }
        return implemented;
    }
    static void required(JsonArray requirements, Graph graph, Map<Node,Shape> scope,
                         Map<Node,Set<Node>> parameters,Map<Node,Set<Node>> implemented,
                         JenaWorker.Control control) {
        for (JsonElement raw:requirements) {
            control.work();JsonObject requirement=raw.getAsJsonObject();
            Node shape=iri(JenaWorker.text(requirement,"shape"));
            Node parameter=iri(JenaWorker.text(requirement,"parameter"));
            JsonElement selected=requirement.get("component");
            Node component=selected==null||selected.isJsonNull()?null:iri(JenaWorker.text(requirement,"component"));
            Set<Node> expected=parameters.get(parameter);
            Set<Node> actual=implemented.get(shape);
            if (!scope.containsKey(shape) || !graph.contains(shape,parameter,Node.ANY)
                || expected==null || actual==null || Collections.disjoint(expected,actual)
                || component!=null && (!expected.contains(component) || !actual.contains(component)))
                throw new JenaWorker.Unavailable("Required parameter has no bound selected native implementation");
        }
    }
    public static JsonObject execute(JsonObject request, JsonObject response,
                                     JenaWorker.Control control,int cap) {
        DatasetGraph dataset=DatasetGraphFactory.createTxnMem();
        RDFParser.fromString(JenaWorker.text(request,"graphs"),Lang.NQUADS)
            .labelToNode(LabelToNode.createUseLabelAsGiven()).parse(dataset);
        var input=dataset.find();
        while (input.hasNext()) { control.work();control.triple();input.next(); }
        Graph graph=dataset.getGraph(iri(ROLE+"shapes"));
        Graph authored=dataset.getGraph(iri(ROLE+"authored-shapes"));
        Graph data=dataset.getGraph(iri(ROLE+"data"));
        if (JenaWorker.flag(request,"has_ontology")) {
            Graph mixed=ModelFactory.createDefaultModel().getGraph();
            for (Graph part:List.of(data,dataset.getGraph(iri(ROLE+"ontology")))) {
                var triples=part.find();
                try { while (triples.hasNext()) {control.work();control.triple();mixed.add(triples.next());} }
                finally {triples.close();}
            }
            data=mixed;
        }
        Set<Node> selected=bindings(dataset,"shape",control);
        Set<Node> focuses=bindings(dataset,"focus",control);
        Shapes shapes;
        Map<Node,Shape> scope;
        try {
            if (!selected.isEmpty()) {
                Shapes original=Shapes.parse(authored);
                for (Node node:selected) if (original.getShape(node)==null)
                    throw new JenaWorker.Unavailable("Selected resource is not an authored native shape");
            }
            shapes=Shapes.parse(graph);
            scope=selected.isEmpty()?shapes.getShapeMap():ShapesParser.parseProcess(graph,selected).shapesMap;
        } catch (JenaWorker.Unavailable error) {throw error;}
        catch (RuntimeException error) {
            throw new JenaWorker.Unavailable("Native selected shape parsing is unavailable: "+error.getClass().getSimpleName());
        }
        Map<Node,Set<Node>> parameters=parameters(control);
        Set<Node> custom=declaredParameters(graph,control);
        Map<Node,Set<Node>> implemented=new HashMap<>();
        for (Shape shape:scope.values()) {
            control.work();control.state();implemented.put(shape.getShapeNode(),components(
                shape,graph,parameters,custom,JenaWorker.flag(request,"trusted_sparql"),control));
        }
        required(request.getAsJsonArray("required_constraints"),graph,scope,parameters,implemented,control);
        ValidationContext context=ValidationContext.create(shapes,data);
        context.setStrict(true);
        Model coverage=ModelFactory.createDefaultModel();
        boolean pairs=JenaWorker.flag(request,"pair_mode");
        Collection<Shape> roots=new ArrayList<>();
        if (selected.isEmpty()) {
            for (Shape shape:shapes.getTargetShapes()) {control.state();roots.add(shape);}
        } else for (Node node:selected) {control.state();roots.add(scope.get(node));}
        for (Shape shape:roots) {
            control.work();
            if (shape==null) throw new JenaWorker.Unavailable("A selected native shape is unavailable");
            if (shape.deactivated()) {
                control.triple();coverage.getGraph().add(Triple.create(iri(COVERAGE+"scope"),iri(COVERAGE+"deactivated"),shape.getShapeNode()));
                continue;
            }
            Set<Node> actual=new LinkedHashSet<>();
            if (pairs) {
                for (Node node:focuses) {control.state();actual.add(node);}
            } else for (var target:shape.getTargets())
                for (Node node:target.getFocusNodes(data)) {
                    control.work();control.state();if (focuses.isEmpty()||focuses.contains(node)) actual.add(node);
                }
            for (Node focus:actual) {
                control.work();ValidationProc.execValidateShape(context,data,shape,focus);
                control.state();control.triple();control.triple();Node record=NodeFactory.createBlankNode();
                coverage.getGraph().add(Triple.create(record,iri(COVERAGE+"shape"),shape.getShapeNode()));
                coverage.getGraph().add(Triple.create(record,iri(COVERAGE+"focus"),focus));
            }
        }
        ValidationReport report=context.generateReport();
        String serialized=serialize(report.getModel(),cap,control);
        JsonObject targets=new JsonObject();
        targets.addProperty("graph",serialize(coverage,cap,control));
        targets.addProperty("complete",true);targets.addProperty("exhausted",false);targets.addProperty("message","");
        response.addProperty("status","result");response.addProperty("conforms",report.conforms());
        response.addProperty("shapes_count",scope.size());response.addProperty("report",serialized);
        response.add("coverage",targets);response.add("warnings",new JsonArray());
        return response;
    }
}
