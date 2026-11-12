"""A tiny scripted language server for tests (stdio JSON-RPC, no dependencies).

Answers about the word under the cursor in the file the client opened, so tests
can check positions, UTF-16 columns and that edits on disk are re-sent:
- definition → the first line of the file that contains `def <word>` (a LocationLink)
- references → every line containing the word
- hover → the word, the column it got and how many lines the file has
- documentSymbol → a class with a method that has a local variable
- workspace/symbol → the query as a function in the first opened file
- diagnostics are pushed on open/change: one warning per line containing TODO
Before answering `initialize` it asks the client for configuration, which a
client must answer or the server waits forever. `CRASH` in a hover word exits.
"""

import json
import re
import sys

docs = {}


def send(msg):
    body = json.dumps({"jsonrpc": "2.0", **msg}).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()


def read():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        line = line.strip()
        if not line:
            break
        k, _, v = line.decode().partition(":")
        if k.lower() == "content-length":
            length = int(v)
    return json.loads(sys.stdin.buffer.read(length))


def utf16_to_index(text, col):
    units = 0
    for i, ch in enumerate(text):
        if units >= col:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


def word_at(uri, pos):
    line = docs[uri].splitlines()[pos["line"]]
    i = utf16_to_index(line, pos["character"])
    m = re.match(r"\w+", line[i:])
    return m.group(0) if m else ""


def publish(uri, version):
    diags = [{"range": {"start": {"line": n, "character": 0}, "end": {"line": n, "character": 4}},
              "severity": 2, "message": "a TODO is left\nsecond line of detail", "source": "fake"}
             for n, line in enumerate(docs[uri].splitlines()) if "TODO" in line]
    send({"method": "textDocument/publishDiagnostics",
          "params": {"uri": uri, "version": version, "diagnostics": diags}})


def rng(line, col, length):
    return {"start": {"line": line, "character": col}, "end": {"line": line, "character": col + length}}


while True:
    msg = read()
    method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
    if method is None:
        continue  # a response to our configuration request
    if method == "initialize":
        send({"id": 99, "method": "workspace/configuration", "params": {"items": [{}]}})
        send({"id": rid, "result": {"capabilities": {"workspaceSymbolProvider": True}}})
    elif method == "textDocument/didOpen":
        td = params["textDocument"]
        docs[td["uri"]] = td["text"]
        publish(td["uri"], td["version"])
    elif method == "textDocument/didChange":
        uri = params["textDocument"]["uri"]
        docs[uri] = params["contentChanges"][-1]["text"]
        publish(uri, params["textDocument"]["version"])
    elif method == "textDocument/hover":
        uri, pos = params["textDocument"]["uri"], params["position"]
        word = word_at(uri, pos)
        if word == "CRASH":
            sys.exit(3)
        send({"id": rid, "result": {"contents": {"kind": "markdown", "value":
              f"**{word}** at character {pos['character']}; "
              f"{len(docs[uri].splitlines())} lines"}}})
    elif method == "textDocument/definition":
        uri, word = params["textDocument"]["uri"], word_at(params["textDocument"]["uri"], params["position"])
        hits = [n for n, line in enumerate(docs[uri].splitlines()) if f"def {word}" in line]
        send({"id": rid, "result": [{"targetUri": uri, "targetRange": rng(n, 0, 1),
                                     "targetSelectionRange": rng(n, 4, len(word))} for n in hits]})
    elif method == "textDocument/references":
        uri, word = params["textDocument"]["uri"], word_at(params["textDocument"]["uri"], params["position"])
        send({"id": rid, "result": [{"uri": uri, "range": rng(n, line.index(word), len(word))}
                                    for n, line in enumerate(docs[uri].splitlines()) if word in line]})
    elif method == "textDocument/documentSymbol":
        send({"id": rid, "result": [{"name": "Box", "kind": 5, "range": rng(0, 0, 3),
                                     "selectionRange": rng(0, 6, 3), "children": [
                                         {"name": "open", "kind": 6, "detail": "(self)",
                                          "range": rng(1, 4, 4), "selectionRange": rng(1, 8, 4),
                                          "children": [{"name": "tmp", "kind": 13,
                                                        "range": rng(2, 8, 3),
                                                        "selectionRange": rng(2, 8, 3)}]}]}]})
    elif method == "workspace/symbol":
        uri = next(iter(docs), "file:///nowhere.py")
        send({"id": rid, "result": [{"name": params["query"], "kind": 12, "containerName": "mod",
                                     "location": {"uri": uri, "range": rng(0, 0, 1)}}]})
    elif method == "shutdown":
        send({"id": rid, "result": None})
    elif method == "exit":
        sys.exit(0)
    elif rid is not None:
        send({"id": rid, "error": {"code": -32601, "message": f"Method Not Found: {method}"}})
