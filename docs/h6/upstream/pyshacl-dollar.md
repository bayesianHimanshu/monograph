# Draft: sh:pattern treats `$` as matching before a final newline

Status: draft, not filed. Target: https://github.com/RDFLib/pySHACL/issues

## Summary

pySHACL evaluates `sh:pattern` with Python's `re.search`. In Python, `$` (without the MULTILINE flag)
matches at the end of the string *and* just before a newline that ends the string. SHACL defines
`sh:pattern` by reference to XPath `fn:matches`, where `$` (without the `m` flag) matches only at the
end of the input. So a value with a trailing newline conforms in pySHACL but should not.

## Versions

* pySHACL 0.40.2, rdflib 7.6.0, Python 3.12.

## Minimal reproduction

```python
import pyshacl
from rdflib import RDF, Graph, Literal, URIRef

shapes = Graph().parse(format="turtle", data="""
@prefix sh: <http://www.w3.org/ns/shacl#> . @prefix ex: <http://ex/> .
ex:Code a sh:NodeShape ; sh:targetClass ex:Coded ;
  sh:property [ sh:path ex:code ; sh:pattern "^[A-Z]{3}$" ] .
""")
data = Graph()
node = URIRef("http://ex/c")
data.add((node, RDF.type, URIRef("http://ex/Coded")))
data.add((node, URIRef("http://ex/code"), Literal("ABC\n")))

conforms, _, _ = pyshacl.validate(data, shacl_graph=shapes)
print(conforms)  # prints True; expected False
```

## Expected

`False`: `"ABC\n"` does not match `^[A-Z]{3}$` under XPath semantics (and does not under Java, .NET,
Rust `regex` or Postgres ARE either).

## Actual

`True`.

## Why it matters

Identifier patterns such as `^[0-9A-Z]{10}$` are commonly used to keep keys clean. With this behaviour
a key with a trailing newline passes SHACL while every other validator in the same pipeline rejects
it, so SHACL and the application disagree on the same data.

## Possible fix

Before compiling an `sh:pattern` value, translate unescaped `$` outside character classes to `\Z`
(Python's "end of string only"), unless the `m` flag is given in `sh:flags`. An alternative is
`re.fullmatch`-style anchoring, but that changes the meaning of unanchored patterns, so the `$`
translation is the narrower change.

## Workaround we use

For bounded patterns we also emit `sh:maxLength` (implied by the pattern), and for every string we
emit `sh:not [ sh:pattern "[<control characters>]" ]`, which needs no anchors. Both reject the trailing
newline in pySHACL. The characterization test `tests/test_pyshacl_behaviour.py` will tell us when the
upstream behaviour changes.
