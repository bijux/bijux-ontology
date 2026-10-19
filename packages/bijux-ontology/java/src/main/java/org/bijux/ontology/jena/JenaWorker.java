package org.bijux.ontology.jena;

import com.google.gson.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import org.apache.jena.Jena;

/** One local Jena operation; stdout contains only its bounded protocol receipt. */
public final class JenaWorker {
    private JenaWorker() {}

    public static final class Stopped extends RuntimeException {
        public Stopped(String message) { super(message); }
    }
    public static final class Unavailable extends RuntimeException {
        public Unavailable(String message) { super(message); }
    }
    public static final class Control {
        final long maximumWork, maximumStates, maximumTriples;
        long work, states, triples;
        Control(JsonObject request) {
            maximumWork = number(request, "control_work");
            maximumStates = number(request, "control_states");
            maximumTriples = number(request, "control_triples");
        }
        public void work() {
            if (work >= maximumWork) throw new Stopped("Jena wrapper work reservation exhausted");
            work++;
        }
        public void state() {
            if (states >= maximumStates) throw new Stopped("Jena wrapper state reservation exhausted");
            states++;
        }
        public void triple() {
            if (triples >= maximumTriples) throw new Stopped("Jena wrapper triple reservation exhausted");
            triples++;
        }
        JsonObject receipt() {
            JsonObject result = new JsonObject();
            result.addProperty("work", Math.min(work, maximumWork));
            result.addProperty("states", Math.min(states, maximumStates));
            result.addProperty("triples", Math.min(triples, maximumTriples));
            return result;
        }
    }
    public static final class CappedOutput extends OutputStream {
        final OutputStream destination;
        final int maximum;
        int count;
        boolean exhausted;
        public CappedOutput(OutputStream destination, int maximum) {
            this.destination = destination; this.maximum = maximum;
        }
        @Override public void write(int value) throws IOException {
            if (count >= maximum) { exhausted = true; throw new Stopped("Jena output byte cap exhausted"); }
            destination.write(value); count++;
        }
        @Override public void write(byte[] value, int offset, int length) throws IOException {
            if (length > maximum-count) { exhausted = true; throw new Stopped("Jena output byte cap exhausted"); }
            destination.write(value, offset, length); count += length;
        }
        @Override public void close() throws IOException {
            if (exhausted) throw new Stopped("Jena output byte cap exhausted");
            destination.flush();
        }
    }
    public static String text(JsonObject value, String key) {
        JsonElement raw = value.get(key);
        if (raw == null || !raw.isJsonPrimitive() || !raw.getAsJsonPrimitive().isString())
            throw new IllegalArgumentException("Missing text field: "+key);
        return raw.getAsString();
    }
    public static boolean flag(JsonObject value, String key) {
        JsonElement raw = value.get(key);
        if (raw == null || !raw.isJsonPrimitive() || !raw.getAsJsonPrimitive().isBoolean())
            throw new IllegalArgumentException("Missing boolean field: "+key);
        return raw.getAsBoolean();
    }
    public static int number(JsonObject value, String key) {
        JsonElement raw = value.get(key);
        if (raw == null || !raw.isJsonPrimitive() || !raw.getAsJsonPrimitive().isNumber())
            throw new IllegalArgumentException("Missing integer field: "+key);
        try { return raw.getAsBigDecimal().intValueExact(); }
        catch (ArithmeticException error) { throw new IllegalArgumentException("Invalid integer field: "+key); }
    }
    private static JsonObject identity() {
        JsonObject result = new JsonObject();
        result.addProperty("protocol", 1);
        result.addProperty("jena", Jena.VERSION);
        result.addProperty("java", System.getProperty("java.version"));
        result.addProperty("java_feature", Runtime.version().feature());
        result.addProperty("backend", "jena "+Jena.VERSION+" / Java "+System.getProperty("java.version"));
        return result;
    }
    public static void main(String[] arguments) throws Exception {
        int cap = Integer.parseInt(arguments[0]);
        JsonObject result = identity();
        Control control = null;
        try {
            if (cap < 1024 || cap > 8_000_000) throw new Stopped("Jena protocol cap is too small");
            byte[] raw = System.in.readNBytes(cap*8+1);
            if (raw.length > cap*8) throw new Stopped("Jena request exceeds its protocol cap");
            com.google.gson.stream.JsonReader reader = new com.google.gson.stream.JsonReader(
                new StringReader(new String(raw, StandardCharsets.UTF_8)));
            reader.setStrictness(Strictness.STRICT);
            reader.setNestingLimit(16);
            JsonElement parsed = JsonParser.parseReader(reader);
            if (!parsed.isJsonObject()) throw new IllegalArgumentException("Expected a request object");
            JsonObject request = parsed.getAsJsonObject();
            if (request.size()>32 || number(request,"protocol") != 1 || !text(request,"operation").equals("shacl"))
                throw new IllegalArgumentException("Unsupported Jena operation");
            control = new Control(request);
            result = ShaclValidation.execute(request, result, control, cap);
        } catch (Stopped error) {
            result.addProperty("status", "exhausted"); result.addProperty("message", error.getMessage());
        } catch (Unavailable error) {
            result.addProperty("status", "unavailable"); result.addProperty("message", error.getMessage());
        } catch (StackOverflowError | OutOfMemoryError error) {
            result = identity(); result.addProperty("status", "exhausted");
            result.addProperty("message", "Jena native memory/depth capacity exhausted");
        } catch (RuntimeException error) {
            result.addProperty("status", "failure");
            String message = error.getClass().getSimpleName()+": "+error.getMessage();
            result.addProperty("message", message.substring(0,Math.min(2000,message.length())));
        }
        if (control != null) result.add("controlled", control.receipt());
        try (CappedOutput output = new CappedOutput(System.out, cap);
             Writer writer = new OutputStreamWriter(output, StandardCharsets.UTF_8)) {
            new GsonBuilder().disableHtmlEscaping().create().toJson(result, writer);
        }
    }
}
