package org.bijux.ontology.modules;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.IOException;
import java.nio.file.*;
import java.util.*;
import org.bijux.ontology.axioms.AxiomConstruction;
import org.bijux.ontology.backend.WorkerProtocol;
import org.bijux.ontology.documents.NativeDocuments;
import org.semanticweb.owlapi.model.*;

/** Exact annotated axiom relocation in one fresh owned manager, never source publication. */
public final class ModuleChanges {
    private ModuleChanges() {}
    private static final class Inventory {
        final int maximum, cap;
        int used;
        Inventory(int maximum,int cap) {this.maximum=maximum;this.cap=cap;}
        void pay(int count)throws IOException {if(count<0||count>maximum-used)throw new WorkerProtocol.OutputLimit();used+=count;}
        String render(Object value)throws IOException {pay(1);return WorkerProtocol.rendering(value,cap);}
    }
    private static final class Move {
        final int source,target;final boolean inline;final Set<OWLAxiom> axioms;
        Move(int source,int target,boolean inline,Set<OWLAxiom> axioms) {
            this.source=source;this.target=target;this.inline=inline;this.axioms=axioms;
        }
    }
    private static int index(JsonNode value,Map<Integer,OWLOntology> modules)throws IOException {
        if(!value.isInt()||value.intValue()==0||!modules.containsKey(value.intValue()))throw new IOException("An original module index is required");
        return value.intValue();
    }
    private static List<JsonNode> records(JsonNode values,Inventory inventory)throws IOException {
        if(!values.isArray())throw new IOException("A finite module inventory is required");inventory.pay(values.size());
        List<JsonNode> result=new ArrayList<>();for(JsonNode value:values)result.add(value);return result;
    }
    private static Set<OWLAxiom> construct(JsonNode values,AxiomConstruction factory,Inventory inventory)throws IOException {
        Set<OWLAxiom> axioms=new LinkedHashSet<>();
        for(JsonNode value:records(values,inventory))if(!axioms.add(factory.axiom(value)))throw new IOException("Duplicate selected annotated axiom");
        return axioms;
    }
    private static List<Map<String,Object>> scopes(Map<Integer,OWLOntology> modules,
            NativeDocuments.Loaded input,Inventory inventory)throws IOException {
        List<Map<String,Object>> result=new ArrayList<>();
        for(Map.Entry<Integer,OWLOntology> entry:modules.entrySet()) {
            if(entry.getKey()==0)continue;inventory.pay(1);
            Map<String,Object> record=new LinkedHashMap<>();record.put("document",entry.getKey());
            List<Integer> closure=new ArrayList<>();
            for(OWLOntology member:entry.getValue().getImportsClosure()) {
                inventory.pay(1);Integer owner=input.owners.get(member.getOWLOntologyManager().getOntologyDocumentIRI(member).toString());
                if(owner==null||owner==0)throw new IOException("An original depends on the synthetic joint root");closure.add(owner);
            }
            List<String> imports=new ArrayList<>();
            for(OWLImportsDeclaration declaration:entry.getValue().getImportsDeclarations())imports.add(inventory.render(declaration.getIRI()));
            record.put("closure",closure);record.put("imports",imports);result.add(record);
        }
        return result;
    }
    private static long bytes(Map<String,Object> receipt) {
        long result=0;
        for(Object raw:(List<?>)receipt.get("modules"))result+=((Number)((Map<?,?>)raw).get("bytes")).longValue();
        return result;
    }

    /** Validate every owner and removal before applying the complete in-memory change set. */
    public static Map<String,Object> evaluate(JsonNode request,Path job,Map<String,Object> result)throws Exception {
        int cap=WorkerProtocol.positive(request.path("max_bytes"));
        int maximum=WorkerProtocol.positive(request.path("max_nodes"));
        int issues=WorkerProtocol.positive(request.path("max_issues"));
        int resources=WorkerProtocol.positive(request.path("max_resources"));
        Inventory inventory=new Inventory(maximum,cap);result.put("qualified",false);
        List<Map<String,Object>> losses=new ArrayList<>();result.put("changes",losses);
        NativeDocuments.Loaded input=NativeDocuments.loadRequest(request,job);
        input.qualifyOriginals(losses,cap,issues);if(!losses.isEmpty())return result;
        OWLOntologyManager manager=input.ontology.getOWLOntologyManager();OWLDataFactory f=manager.getOWLDataFactory();
        Map<Integer,OWLOntology> modules=new TreeMap<>();Map<Integer,Set<OWLAxiom>> before=new HashMap<>();
        Map<Integer,Set<OWLImportsDeclaration>> beforeImports=new HashMap<>();
        for(OWLOntology module:manager.getOntologies()) {
            inventory.pay(1+module.getAxiomCount());Integer owner=input.owners.get(manager.getOntologyDocumentIRI(module).toString());
            if(owner==null||modules.put(owner,module)!=null)throw new IOException("Ambiguous module owner");
            before.put(owner,new HashSet<>(module.getAxioms()));beforeImports.put(owner,new HashSet<>(module.getImportsDeclarations()));
        }
        if(modules.size()!=request.path("documents").size())throw new IOException("The exact source set was not loaded");
        result.put("before_scopes",scopes(modules,input,inventory));
        Set<Integer> owned=new LinkedHashSet<>();
        for(JsonNode raw:records(request.path("owned"),inventory))if(!owned.add(index(raw,modules)))throw new IOException("Duplicate owned module");
        if(owned.isEmpty())throw new IOException("No owned modules selected");
        AxiomConstruction factory=new AxiomConstruction(f,maximum,WorkerProtocol.positive(request.path("projection_depth")),cap);
        Map<Integer,Set<OWLAxiom>> protectedAxioms=new HashMap<>();
        for(JsonNode guarded:records(request.path("protected"),inventory)) {
            int owner=index(guarded.path("document"),modules);Set<OWLAxiom> axioms=construct(guarded.path("axioms"),factory,inventory);
            if(protectedAxioms.put(owner,axioms)!=null||!before.get(owner).containsAll(axioms)) {
                result.put("failure","protected_precondition");return result;
            }
        }
        List<Move> moves=new ArrayList<>();Map<Integer,Set<OWLAxiom>> removals=new HashMap<>();
        for(JsonNode raw:records(request.path("moves"),inventory)) {
            int source=index(raw.path("source"),modules),target=index(raw.path("target"),modules);
            if(source==target||!owned.contains(source)||!owned.contains(target)||!raw.path("inline").isBoolean())throw new IOException("Move ownership is incomplete");
            boolean inline=raw.path("inline").booleanValue();Set<OWLAxiom> axioms;
            if(inline){inventory.pay(before.get(source).size());axioms=new HashSet<>(before.get(source));}
            else axioms=construct(raw.path("axioms"),factory,inventory);
            if(axioms.isEmpty()||!before.get(source).containsAll(axioms)) {result.put("failure","move_precondition");return result;}
            Set<OWLAxiom> selected=removals.computeIfAbsent(source,key->new HashSet<>());
            for(OWLAxiom axiom:axioms) {
                inventory.pay(1);
                if(!selected.add(axiom)||protectedAxioms.getOrDefault(source,Collections.emptySet()).contains(axiom)) {result.put("failure","protected_or_repeated_removal");return result;}
            }
            moves.add(new Move(source,target,inline,axioms));
        }
        if(moves.isEmpty())throw new IOException("No actual module changes selected");
        List<OWLOntologyChange> imports=new ArrayList<>();Set<Integer> importOwners=new HashSet<>();
        for(JsonNode raw:records(request.path("imports"),inventory)) {
            int owner=index(raw.path("document"),modules);if(!owned.contains(owner)||!importOwners.add(owner))throw new IOException("Import changes lack unique ownership");
            Set<IRI> seen=new HashSet<>();
            for(String key:List.of("additions","removals"))for(JsonNode value:records(raw.path(key),inventory)) {
                IRI iri=IRI.create(WorkerProtocol.text(value,cap));if(!iri.isAbsolute()||!seen.add(iri))throw new IOException("Ambiguous import delta");
                OWLImportsDeclaration declaration=f.getOWLImportsDeclaration(iri);boolean add=key.equals("additions");
                if(add==beforeImports.get(owner).contains(declaration)) {result.put("failure","import_precondition");return result;}
                if(add&&!request.path("aliases").has(iri.toString()))throw new IOException("Added import lacks a caller-supplied alias");
                imports.add(add?new AddImport(modules.get(owner),declaration):new RemoveImport(modules.get(owner),declaration));
            }
        }
        Path beforeDirectory=job.resolve("before-rdf");Files.createDirectory(beforeDirectory);
        Map<String,Object> beforeViews=NativeDocuments.checkedRdfModules(input,beforeDirectory,cap,issues,resources);
        result.put("before_views",beforeViews);if(!Boolean.TRUE.equals(beforeViews.get("qualified"))){result.put("changes",beforeViews.get("changes"));return result;}
        long written=bytes(beforeViews);
        for(Move move:moves)manager.removeAxioms(modules.get(move.source),move.axioms);
        for(Move move:moves)manager.addAxioms(modules.get(move.target),move.axioms);
        manager.applyChanges(imports);
        for(int owner:owned) {
            OWLOntology module=modules.get(owner);inventory.pay(module.getSignature().size()+module.getAnnotations().size());
            Set<OWLEntity> signature=new HashSet<>(module.getSignature());
            for(OWLAnnotation annotation:module.getAnnotations()){inventory.pay(annotation.getSignature().size());signature.addAll(annotation.getSignature());}
            for(OWLEntity entity:signature)if(!entity.isBuiltIn())manager.addAxiom(module,f.getOWLDeclarationAxiom(entity));
        }
        for(Map.Entry<Integer,Set<OWLAxiom>> guarded:protectedAxioms.entrySet())
            if(!modules.get(guarded.getKey()).getAxioms().containsAll(guarded.getValue())) {result.put("failure","protected_postcondition");return result;}
        for(Move move:moves)if(!move.inline)for(OWLAxiom axiom:move.axioms)
            if(modules.get(move.source).containsAxiom(axiom)) {result.put("failure","required_source_declaration");return result;}
        for(int owner:modules.keySet())if(!owned.contains(owner)&&(!before.get(owner).equals(modules.get(owner).getAxioms())||!beforeImports.get(owner).equals(modules.get(owner).getImportsDeclarations())))throw new IOException("Read-only module changed");
        result.put("after_scopes",scopes(modules,input,inventory));
        List<Map<String,Object>> edits=new ArrayList<>();
        for(int owner:owned) {
            Map<String,Object> edit=new LinkedHashMap<>();edit.put("document",owner);
            List<String> added=new ArrayList<>(),removed=new ArrayList<>();
            for(OWLAxiom axiom:modules.get(owner).getAxioms())if(!before.get(owner).contains(axiom))added.add(inventory.render(axiom));
            for(OWLAxiom axiom:before.get(owner))if(!modules.get(owner).containsAxiom(axiom))removed.add(inventory.render(axiom));
            edit.put("additions",added);edit.put("removals",removed);edits.add(edit);
        }
        result.put("edits",edits);
        if(written>=cap)throw new WorkerProtocol.OutputLimit();
        Map<String,Object> output=NativeDocuments.checkedWriteModules(input,job,owned,request.path("profiles"),(int)(cap-written),issues,resources);
        written+=bytes(output);result.putAll(output);result.put("qualified",false);
        if(!Boolean.TRUE.equals(output.get("qualified")))return result;
        if(written>=cap)throw new WorkerProtocol.OutputLimit();
        Path afterDirectory=job.resolve("after-rdf");Files.createDirectory(afterDirectory);
        Map<String,Object> afterViews=NativeDocuments.checkedRdfModules(input,afterDirectory,(int)(cap-written),issues,resources);
        result.put("after_views",afterViews);result.put("qualified",Boolean.TRUE.equals(afterViews.get("qualified")));
        if(!Boolean.TRUE.equals(afterViews.get("qualified")))result.put("changes",afterViews.get("changes"));
        return result;
    }
}
