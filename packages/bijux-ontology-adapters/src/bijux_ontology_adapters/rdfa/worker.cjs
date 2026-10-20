/* Owned bounded HTML5 producer using the normally installed maintained parser. */
'use strict';
const path = require('node:path');
const fs = require('node:fs');
const cap = Number(process.argv[2]);
const root = process.argv[3];
const chunks = [];
let inputSize = 0;
let sent = false;
const kinds = ['bytes', 'states', 'work', 'triples', 'resources', 'issues'];
const consumed = Object.fromEntries(kinds.map(k => [k, 0]));
let allowances = null;
let outputCap = cap;
let producer = null;
const quads = [];
const diagnostics = [];
let unsupportedCopy = false;
let wireBytes = 768;
class Exhausted extends Error {}
function pay(kind, amount = 1) {
  if (!Number.isSafeInteger(amount) || amount < 0 || consumed[kind] + amount > allowances[kind]) {
    throw new Exhausted('Native RDFa exceeded its ' + kind + ' allowance');
  }
  consumed[kind] += amount;
}
function text(value) {
  if (typeof value !== 'string') throw new Error('Invalid native RDFJS text');
  pay('work', value.length + 1);
  pay('bytes', 64 + value.length * 6);
  return value;
}
function reply(status) {
  if (sent) return;
  sent = true;
  const result = {protocol: 1, status, producer, consumed, quads, diagnostics, unsupportedCopy};
  const data = JSON.stringify(result);
  if (Buffer.byteLength(data) > outputCap) {
    process.stderr.write('RDFa reply exceeded its reserved bound');
    process.exitCode = 2;
  } else process.stdout.write(data);
}
function term(value) {
  if (value.termType === 'DefaultGraph') return ['DefaultGraph', ''];
  if (value.termType === 'Literal') {
    return ['Literal', text(value.value), text(value.language), text(value.datatype.value)];
  }
  if (!['NamedNode', 'BlankNode'].includes(value.termType)) throw new Error('Unsupported RDFJS term');
  return [value.termType, text(value.value)];
}
function unavailable(message) {
  diagnostics.push(message);
  reply('unavailable');
}
process.stdin.on('data', chunk => {
  inputSize += chunk.length;
  if (!Number.isSafeInteger(cap) || cap <= 0 || inputSize > cap) {
    process.stderr.write('RDFa request exceeded its bound');
    process.exit(2);
  }
  chunks.push(chunk);
});
process.stdin.on('end', () => {
  let parser;
  try {
    const request = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (request.protocol !== 1 || request.host !== 'html5' || typeof request.text !== 'string' ||
        typeof request.document_url !== 'string' || !Number.isSafeInteger(request.max_depth) ||
        !Number.isSafeInteger(request.output_cap) || request.output_cap <= 0 || request.output_cap > cap ||
        !request.allowances || kinds.some(k => !Number.isSafeInteger(request.allowances[k]) || request.allowances[k] < 0)) {
      throw new Error('Invalid RDFa request');
    }
    allowances = request.allowances;
    outputCap = request.output_cap;
    const packagePath = path.join(root, 'node_modules/rdfa-streaming-parser/package.json');
    let packageData;
    try {
      const fd = fs.openSync(packagePath, 'r');
      try {
        if (fs.fstatSync(fd).size > 65536) throw new Error('Package metadata exceeds its bound');
        const buffer = Buffer.alloc(65537);
        const size = fs.readSync(fd, buffer, 0, buffer.length, 0);
        if (size > 65536) throw new Error('Package metadata exceeds its bound');
        packageData = JSON.parse(buffer.toString('utf8', 0, size));
      } finally { fs.closeSync(fd); }
    }
    catch (_) { unavailable('RDFa needs npm ci with the shipped locked runtime assets'); return; }
    if (packageData.version !== '3.0.2') {
      unavailable('RDFa requires rdfa-streaming-parser 3.0.2'); return;
    }
    let RdfaParser;
    try { ({RdfaParser} = require(path.join(root, 'node_modules/rdfa-streaming-parser'))); }
    catch (_) { unavailable('RDFa locked runtime dependencies are unavailable'); return; }
    producer = {node: process.version, parser: packageData.version, host: 'html5'};
    const denied = () => { throw new Error('Implicit RDFa retrieval is forbidden'); };
    for (const name of ['node:http', 'node:https']) {
      const module = require(name); module.get = denied; module.request = denied;
    }
    require('node:net').connect = denied;
    require('node:net').createConnection = denied;
    globalThis.fetch = denied;
    let depth = 0;
    const listener = {
      onTagOpen(name, attributes) {
        pay('work'); pay('states');
        if (++depth > request.max_depth) throw new Exhausted('HTML exceeds max_depth');
        text(name);
        for (const [key, value] of Object.entries(attributes)) { pay('work'); text(key); text(value); }
      },
      onTagClose() { pay('work'); depth--; },
      onText(value) { pay('work', value.length + 1); }, // codespell:ignore ontext
      onEnd() { pay('work'); }
    };
    parser = new RdfaParser({baseIRI: request.document_url, profile: 'html', htmlParseListener: listener});
    parser.on('data', quad => {
      try {
        if (quad.predicate.value === 'http://www.w3.org/ns/rdfa#copy') unsupportedCopy = true;
        const estimate = 256 + [quad.subject, quad.predicate, quad.object, quad.graph].reduce(
          (sum, value) => sum + 6 * (value.value.length + (value.language || '').length +
            (value.datatype ? value.datatype.value.length : 0)), 0);
        if (wireBytes + estimate > outputCap) throw new Exhausted('RDFa reply exceeds its reserved bound');
        wireBytes += estimate;
        pay('triples'); pay('work'); pay('states', 5); pay('bytes', 192);
        const row = [term(quad.subject), term(quad.predicate), term(quad.object), term(quad.graph)];
        quads.push(row);
      } catch (error) { parser.destroy(error); }
    });
    parser.on('error', error => {
      if (error instanceof Exhausted) reply('exhausted');
      else {
        try { diagnostics.push(text(String(error))); } catch (_) {}
        reply('input_error');
      }
    });
    parser.on('end', () => reply('result'));
    parser.end(request.text);
  } catch (error) {
    if (parser) parser.destroy();
    if (error instanceof Exhausted) reply('exhausted');
    else {
      if (allowances) {
        try { diagnostics.push(text(String(error))); } catch (_) {}
      }
      reply('input_error');
    }
  }
});
