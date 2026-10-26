package org.bijux.ontology.jena;

import com.google.gson.*;
import java.io.StringReader;
import java.util.*;
import javax.xml.stream.*;
import org.apache.jena.geosparql.configuration.GeoSPARQLConfig;
import org.apache.jena.geosparql.implementation.GeometryWrapper;
import org.apache.jena.geosparql.implementation.datatype.*;

/** Finite local geometry checks; lexical identity is never geometric identity. */
public final class GeometryValidation {
    private GeometryValidation() {}
    private static final String CRS84="http://www.opengis.net/def/crs/OGC/1.3/CRS84";
    private static final String EPSG4326="http://www.opengis.net/def/crs/EPSG/0/4326";
    private static final String WKT="http://www.opengis.net/ont/geosparql#wktLiteral";
    private static final String GML="http://www.opengis.net/ont/geosparql#gmlLiteral";
    private static final String GML_NS="http://www.opengis.net/gml/3.2";
    private static final String DEGREE="http://www.opengis.net/def/uom/OGC/1.0/degree";

    private static JsonArray array(JsonObject value,String key) {
        JsonElement raw=value.get(key);
        if(raw==null||!raw.isJsonArray())throw new IllegalArgumentException("Missing finite geometry inventory");
        return raw.getAsJsonArray();
    }
    private static boolean local(String crs) { return crs.equals(CRS84)||crs.equals(EPSG4326); }
    private static String wktCrs(String text) {
        String body=text.stripLeading();String crs=CRS84;
        if(body.startsWith("<")) {
            int end=body.indexOf('>');
            if(end<0)throw new IllegalArgumentException("Malformed WKT CRS qualifier");
            crs=body.substring(1,end);body=body.substring(end+1).stripLeading();
        }
        if(!local(crs))throw new JenaWorker.Unavailable("No explicitly local CRS resource for this identifier");
        if(!body.isEmpty()) {
            int end=0;while(end<body.length()&&Character.isLetter(body.charAt(end)))end++;
            String type=body.substring(0,end).toUpperCase(Locale.ROOT);
            if(!Set.of("POINT","LINESTRING","POLYGON").contains(type))
                throw new JenaWorker.Unavailable("WKT geometry type is outside the finite Point/LineString/Polygon profile");
        }
        return crs;
    }
    private static String gmlCrs(String text,JenaWorker.Control control) {
        XMLInputFactory factory=XMLInputFactory.newFactory();
        factory.setProperty(XMLInputFactory.SUPPORT_DTD,false);
        factory.setProperty("javax.xml.stream.isSupportingExternalEntities",false);
        factory.setXMLResolver((publicId,systemId,base,namespace)->{throw new XMLStreamException("External XML resolution is unavailable");});
        XMLStreamReader reader=null;int depth=0,points=0,positions=0,tokens=0;boolean token=false;String crs=null;
        try {
            reader=factory.createXMLStreamReader(new StringReader(text));
            while(reader.hasNext()) {
                control.work();int event=reader.next();
                if(event==XMLStreamConstants.DTD||event==XMLStreamConstants.ENTITY_REFERENCE||event==XMLStreamConstants.PROCESSING_INSTRUCTION)
                    throw new JenaWorker.Unavailable("GML declarations and entity processing are unsupported");
                if(event==XMLStreamConstants.START_ELEMENT) {
                    depth++;control.state();
                    if(!GML_NS.equals(reader.getNamespaceURI()))throw new JenaWorker.Unavailable("Only the declared GML3.2 namespace is supported");
                    if(depth==1&&reader.getLocalName().equals("Point")) {
                        points++;
                        if(reader.getAttributeCount()!=1||!reader.getAttributeLocalName(0).equals("srsName")||reader.getAttributeNamespace(0)!=null)
                            throw new JenaWorker.Unavailable("GML Point requires only an explicit srsName");
                        crs=reader.getAttributeValue(0);
                        if(!local(crs))throw new JenaWorker.Unavailable("No explicitly local GML CRS resource");
                    } else if(depth==2&&reader.getLocalName().equals("pos")&&reader.getAttributeCount()==0)positions++;
                    else throw new JenaWorker.Unavailable("GML structure is outside the one-Point/one-pos profile");
                } else if(event==XMLStreamConstants.CHARACTERS||event==XMLStreamConstants.CDATA) {
                    for(int i=0;i<reader.getTextLength();i++) {
                        control.work();char c=reader.getTextCharacters()[reader.getTextStart()+i];
                        if(depth==2) {if(Character.isWhitespace(c))token=false;else if(!token){tokens++;token=true;}}
                        else if(!Character.isWhitespace(c))throw new JenaWorker.Unavailable("GML has unaccounted text outside pos");
                    }
                } else if(event==XMLStreamConstants.END_ELEMENT)depth--;
            }
            if(points!=1||positions!=1||tokens!=2)throw new JenaWorker.Unavailable("GML Point must have one two-dimensional position");
            return crs;
        } catch(XMLStreamException error) {
            throw new IllegalArgumentException("Malformed XML in supported GML geometry");
        } finally { if(reader!=null)try{reader.close();}catch(XMLStreamException ignored){} }
    }
    private static JsonObject receipt(int index) {JsonObject out=new JsonObject();out.addProperty("index",index);return out;}
    private static void unavailable(JsonObject row,String message) {row.addProperty("status","unavailable");row.addProperty("message",message);}

    public static JsonObject execute(JsonObject request,JsonObject result,JenaWorker.Control control,int cap) {
        GeoSPARQLConfig.setupNoIndex(false);
        JsonArray inputs=array(request,"geometries"),relations=array(request,"relations"),distances=array(request,"distances");
        JsonArray valuesOut=new JsonArray(),relationsOut=new JsonArray(),distancesOut=new JsonArray();
        result.add("geometries",valuesOut);result.add("relations",relationsOut);result.add("distances",distancesOut);
        Map<String,GeometryWrapper> values=new HashMap<>();Set<String> keys=new HashSet<>();
        for(int i=0;i<inputs.size();i++) {
            control.work();control.state();JsonObject input=inputs.get(i).getAsJsonObject();String key=JenaWorker.text(input,"key");
            if(!keys.add(key))throw new IllegalArgumentException("Duplicate geometry key");
            JsonObject row=receipt(i);GeometryWrapper geometry=null;
            try {
                String text=JenaWorker.text(input,"lexical");
                if(input.get("datatype").isJsonNull()||!input.get("language").isJsonNull())throw new JenaWorker.Unavailable("Geometry requires its exact supported RDF datatype without language");
                String datatype=JenaWorker.text(input,"datatype"),crs;
                if(datatype.equals(WKT)) {crs=wktCrs(text);geometry=WKTDatatype.INSTANCE.read(text);}
                else if(datatype.equals(GML)) {crs=gmlCrs(text,control);geometry=GMLDatatype.INSTANCE.read(text);}
                else throw new JenaWorker.Unavailable("Geometry serialization is not supported");
                if(!geometry.isSRSRecognised()||!crs.equals(geometry.getSrsURI()))throw new JenaWorker.Unavailable("Native CRS interpretation is not qualified");
                if(geometry.getCoordinateDimension()!=2)throw new JenaWorker.Unavailable("Only two-dimensional geometry is supported");
                row.addProperty("lexical_valid",true);row.addProperty("geometry_valid",geometry.isValid());
                row.addProperty("crs",crs);row.addProperty("axis_order",geometry.getSrsInfo().isAxisXY()?"longitude_latitude":"latitude_longitude");
                row.addProperty("dimensions",geometry.getCoordinateDimension());row.addProperty("empty",geometry.isEmpty());
                if(geometry.isValid()) {row.addProperty("status","result");values.put(key,geometry);}
                else {row.addProperty("status","invalid_geometry");row.addProperty("message","Native geometry validity check failed");}
            } catch(JenaWorker.Stopped error) {throw error;}
              catch(JenaWorker.Unavailable error) {unavailable(row,error.getMessage());}
              catch(IllegalArgumentException error) {row.addProperty("status","invalid_lexical");row.addProperty("lexical_valid",false);row.addProperty("message","Native parser rejected the supported geometry lexical form");}
              catch(RuntimeException error) {unavailable(row,"Native geometry interpretation is unavailable: "+error.getClass().getSimpleName());}
            valuesOut.add(row);
        }
        for(int kind=0;kind<2;kind++) {
            JsonArray obligations=kind==0?relations:distances, output=kind==0?relationsOut:distancesOut;
            for(int i=0;i<obligations.size();i++) {
                control.work();control.state();JsonObject requestRow=obligations.get(i).getAsJsonObject();JsonObject row=receipt(i);
                GeometryWrapper a=values.get(JenaWorker.text(requestRow,"left")),b=values.get(JenaWorker.text(requestRow,"right"));
                try {
                    if(a==null||b==null)throw new JenaWorker.Unavailable("Spatial operation depends on an unqualified geometry value");
                    if(kind==0) {
                        boolean value=switch(JenaWorker.text(requestRow,"relation")) {
                            case "within"->a.within(b);case "contains"->a.contains(b);case "intersects"->a.intersects(b);
                            case "disjoint"->a.disjoint(b);case "topological_equals"->a.equalsTopo(b);
                            default->throw new JenaWorker.Unavailable("Spatial relation is unsupported");
                        };
                        row.addProperty("value",value);
                    } else {
                        String unit=JenaWorker.text(requestRow,"unit");
                        if(!unit.equals(DEGREE))throw new JenaWorker.Unavailable("Only explicit angular degree planar distance is supported for local geographic CRS");
                        double distance=a.distanceEuclidean(b,unit);
                        if(!Double.isFinite(distance))throw new JenaWorker.Unavailable("Native distance was not finite");
                        row.addProperty("value",distance);
                    }
                    row.addProperty("status","result");
                } catch(JenaWorker.Stopped error) {throw error;}
                  catch(JenaWorker.Unavailable error) {unavailable(row,error.getMessage());}
                  catch(Exception error) {unavailable(row,"Native spatial operation or local transformation is unavailable: "+error.getClass().getSimpleName());}
                output.add(row);
            }
        }
        result.addProperty("status","result");return result;
    }
}
