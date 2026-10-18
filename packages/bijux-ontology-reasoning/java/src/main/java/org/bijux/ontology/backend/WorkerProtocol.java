package org.bijux.ontology.backend;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.IOException;
import java.util.*;
import org.semanticweb.owlapi.profiles.*;
import org.semanticweb.owlapi.util.VersionInfo;

/** Typed shared request/result boundaries for the explicit pinned native worker. */
public final class WorkerProtocol {
    private WorkerProtocol() {}
    private static final List<String> PROFILES = Arrays.asList("owl2-dl", "owl2-el", "owl2-ql", "owl2-rl");

    public static final class OutputLimit extends IOException {}

    public static String text(JsonNode node, int maximum) throws IOException {
        if (!node.isTextual() || node.textValue().length() > maximum) throw new IOException("Invalid text field");
        return node.textValue();
    }

    public static int positive(JsonNode node) throws IOException {
        if (!node.isIntegralNumber() || !node.canConvertToInt() || node.intValue() <= 0) throw new IOException("Invalid positive bound");
        return node.intValue();
    }

    public static String rendering(Object value, int maximum) throws IOException {
        if (value == null) return null;
        String result = value.toString();
        if (result.length() > maximum) throw new OutputLimit();
        return result;
    }

    public static OWLProfile profile(String selected) throws IOException {
        switch (selected) {
            case "owl2-dl": return new OWL2DLProfile();
            case "owl2-el": return new OWL2ELProfile();
            case "owl2-ql": return new OWL2QLProfile();
            case "owl2-rl": return new OWL2RLProfile();
            default: throw new IOException("Unsupported profile");
        }
    }

    public static Map<String,Object> identity(JsonNode request) throws IOException {
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
}
