"""Writes fixtures/valid, fixtures/invalid and fixtures/probes. Run: python tools/make_curated_fixtures.py .

The JSON files are the curated artifact and are committed; this script is kept so they can be audited
and rebuilt. Every invalid case breaks one constraint of a valid record.
"""

import copy
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
B = "https://example.org/monograph/"
def sub(u): return B + "substance/" + u
def org(s): return B + "organization/" + s
def prod(n): return B + "product/" + n
def cdrug(r): return B + "clinical-drug/" + r

valid = {
 "Substance": [
  ("aspirin", {"unii": "R16CO5Y76E", "name": "ASPIRIN"}),
  ("acetaminophen", {"unii": "362O9ITL9D", "name": "ACETAMINOPHEN"}),
  ("ibuprofen", {"unii": "WK2XYI10QM", "name": "IBUPROFEN"}),
 ],
 "Organization": [
  ("bayer", {"slug": "bayer-healthcare-llc", "name": "Bayer HealthCare LLC"}),
  ("acme", {"slug": "acme-pharma-inc", "name": "Acme Pharma, Inc."}),
 ],
 "ClinicalDrug": [
  ("named", {"rxcui": "243670", "name": "aspirin 81 MG Chewable Tablet"}),
  ("unnamed", {"rxcui": "198440"}),
  ("combination", {"rxcui": "2001467"}),
 ],
 "Product": [
  ("aspirin-81", {"product_ndc": "0280-2000", "product_type": "HUMAN OTC DRUG", "finished": True, "name": "Bayer Low Dose Aspirin", "dosage_form": "TABLET, CHEWABLE",
    "routes": ["ORAL"], "clinical_drug": cdrug("243670"),
    "ingredients": [{"substance": sub("R16CO5Y76E"), "strength_value": "81", "strength_unit": "mg/1"}]}),
  ("apap-ibu", {"product_ndc": "12345-6789", "product_type": "HUMAN PRESCRIPTION DRUG", "finished": True, "name": "Acme Dual Action", "dosage_form": "TABLET, FILM COATED",
    "routes": ["ORAL"], "clinical_drug": cdrug("2001467"), "ingredients": [
      {"substance": sub("362O9ITL9D"), "strength_value": "250", "strength_unit": "mg/1"},
      {"substance": sub("WK2XYI10QM"), "strength_value": "125.0", "strength_unit": "mg/1"}]}),
  ("apap-500", {"product_ndc": "12345-678", "product_type": "HUMAN OTC DRUG", "finished": True, "name": "Acme Acetaminophen", "dosage_form": "TABLET",
    "routes": ["ORAL", "RECTAL"], "clinical_drug": cdrug("198440"),
    "ingredients": [{"substance": sub("362O9ITL9D"), "strength_value": "500", "strength_unit": "mg/1"}]}),
  ("no-clinical-drug", {"product_ndc": "55555-0001", "product_type": "HUMAN OTC DRUG", "finished": True, "name": "placebo topical", "dosage_form": "CREAM",
    "routes": ["TOPICAL"]}),
  ("bulk-without-route", {"product_ndc": "55555-0002", "product_type": "BULK INGREDIENT", "finished": False,
    "name": "acetaminophen bulk powder", "dosage_form": "POWDER",
    "ingredients": [{"substance": sub("362O9ITL9D"), "strength_value": "1", "strength_unit": "kg/kg"}]}),
 ],
 "PackagedProduct": [
  ("4-4-2", {"ndc": "0280-2000-10", "product": prod("0280-2000"), "labeler": org("bayer-healthcare-llc"), "description": "36 TABLET, CHEWABLE in 1 BOTTLE"}),
  ("5-3-2", {"ndc": "12345-678-90", "product": prod("12345-678"), "labeler": org("acme-pharma-inc"), "description": "100 TABLET in 1 BOTTLE"}),
  ("5-4-1", {"ndc": "12345-6789-0", "product": prod("12345-6789"), "labeler": org("acme-pharma-inc")}),
 ],
 "Label": [
  ("with-sections", {"set_id": "0c3d2f4e-1a2b-4c5d-8e9f-0a1b2c3d4e5f", "version": 3, "effective_date": "2024-05-01",
    "products": [prod("0280-2000")], "sections": [
      {"code": "34067-9", "title": "INDICATIONS AND USAGE", "text": "For the temporary relief of minor aches and pains."},
      {"code": "34068-7", "title": "DOSAGE AND ADMINISTRATION", "text": "Chew 1 to 4 tablets every 4 hours.\nDo not exceed 48 tablets in 24 hours."}]}),
  ("bare", {"set_id": "9b1e6c2a-3d4f-4a5b-9c6d-7e8f9a0b1c2d", "version": 1, "effective_date": "2019-12-31"}),
 ],
 "AdverseEventReport": [
  ("serious", {"report_id": "10003301", "source_system": "openfda_e2b", "receipt_date": "2024-01-02", "serious": True, "seriousness_criteria": ["hospitalization", "other"],
    "patient_age_years": "64.5", "drugs": [
      {"drug_name": "ASPIRIN 81MG", "clinical_drug": cdrug("243670"), "role": "primary suspect"},
      {"drug_name": "Tylenol", "role": "concomitant"}],
    "reactions": [{"term": "Nausea"}, {"term": "Vomiting"}, {"term": "Nausea"}]}),
  ("non-serious", {"report_id": "20004402-1", "source_system": "faers_qde", "receipt_date": "2023-07-15", "serious": False,
    "drugs": [{"drug_name": "IBUPROFEN", "role": "suspect"}, {"drug_name": "IBUPROFEN", "role": "suspect"}],
    "reactions": [{"term": "Headache"}]}),
 ],
 "Recall": [
  ("class-ii", {"recall_number": "D-0123-2025", "classification": "II", "reason": "Failed dissolution specifications.",
    "report_date": "2025-02-12", "products": [prod("12345-678"), prod("12345-6789")], "recalling_firm": org("acme-pharma-inc")}),
  ("class-i", {"recall_number": "D-0456-2024", "classification": "I", "reason": "Microbial contamination.", "report_date": "2024-03-06",
    "recalling_firm": org("bayer-healthcare-llc")}),
  ("three-digit-number", {"recall_number": "D-321-2016", "classification": "II", "reason": "Lack of assurance of sterility.",
    "report_date": "2015-11-25", "recalling_firm": org("acme-pharma-inc")}),
 ],
}

def base(cls, i=0): return copy.deepcopy(valid[cls][i][1])
def case(cls, cid, kind, field, mutate, i=0, expect="agree_invalid", note=None):
    r = base(cls, i); mutate(r)
    d = {"id": cid, "violates": kind, "field": field, "expect": expect, "record": r}
    if note: d["note"] = note
    invalid.setdefault(cls, []).append(d)
def setk(k, v): return lambda r: r.__setitem__(k, v)
def delk(k): return lambda r: r.pop(k)
invalid = {}
case("Substance", "unii-too-short", "pattern", "unii", setk("unii", "R16CO5Y76"))
case("Substance", "unii-lowercase", "pattern", "unii", setk("unii", "r16co5y76e"))
case("Substance", "name-missing", "required", "name", delk("name"))
case("Substance", "name-not-string", "datatype", "name", setk("name", 42))
case("Substance", "name-empty", "min_length", "name", setk("name", ""))
case("Substance", "name-too-long", "max_length", "name", setk("name", "X" * 501))
case("Substance", "unknown-key", "closed", None, setk("synonym", "ASA"))
case("Organization", "slug-has-space", "pattern", "slug", setk("slug", "Bayer Healthcare"))
case("Organization", "slug-too-long", "max_length", "slug", setk("slug", "a" * 121))
case("Product", "product-ndc-with-package", "pattern", "product_ndc", setk("product_ndc", "0280-2000-10"))
case("Product", "clinical-drug-not-iri", "ref", "clinical_drug", setk("clinical_drug", "243670"))
case("ClinicalDrug", "rxcui-letters", "pattern", "rxcui", setk("rxcui", "RX123"))
case("ClinicalDrug", "name-empty", "min_length", "name", setk("name", ""))
case("ClinicalDrug", "name-has-nul", "charset", "name", setk("name", "aspirin\u0000 81"))
case("Product", "finished-otc-without-route", "rule:finished-rx-otc-requires-route", "routes", setk("routes", []))
case("Product", "unknown-product-type", "enum", "product_type", setk("product_type", "OTC"))
case("Product", "finished-missing", "required", "finished", delk("finished"))
case("Product", "finished-as-string", "datatype", "finished", setk("finished", "Y"))
case("Product", "unknown-route", "enum", "routes", setk("routes", ["ORALLY"]))
case("Product", "ingredient-missing-unit", "node", "ingredients", lambda r: r["ingredients"][0].pop("strength_unit"))
case("Product", "ingredient-not-object", "node", "ingredients", setk("ingredients", ["aspirin 81 mg"]))
case("Product", "strength-zero", "min_exclusive", "ingredients.strength_value", lambda r: r["ingredients"][0].__setitem__("strength_value", "0"))
case("Product", "strength-float", "datatype", "ingredients.strength_value", lambda r: r["ingredients"][0].__setitem__("strength_value", 81.5))
case("Product", "unit-has-newline", "charset", "ingredients.strength_unit", lambda r: r["ingredients"][0].__setitem__("strength_unit", "mg/\n1"))
case("Product", "substance-not-iri", "ref", "ingredients.substance", lambda r: r["ingredients"][0].__setitem__("substance", "R16CO5Y76E"))
case("Product", "duplicate-substance", "pydantic_only:product-ingredient-substances-unique", "ingredients",
     lambda r: r["ingredients"].append({"substance": sub("R16CO5Y76E"), "strength_value": "40", "strength_unit": "mg/1"}), expect="declared_leak",
     note="Postgres rejects this through the UNIQUE generated from the declaration's unique= hint.")
case("PackagedProduct", "ndc-4-4-1", "pattern", "ndc", setk("ndc", "0280-2000-1"))
case("PackagedProduct", "product-missing", "required", "product", delk("product"))
case("PackagedProduct", "labeler-not-iri", "ref", "labeler", setk("labeler", "bayer-healthcare-llc"))
case("PackagedProduct", "description-empty", "min_length", "description", setk("description", ""))
case("Label", "set-id-uppercase", "pattern", "set_id", setk("set_id", "0C3D2F4E-1A2B-4C5D-8E9F-0A1B2C3D4E5F"))
case("Label", "version-zero", "min_inclusive", "version", setk("version", 0))
case("Label", "version-too-big", "max_exclusive", "version", setk("version", 1000000))
case("Label", "version-string", "datatype", "version", setk("version", "3"))
case("Label", "date-openfda-format", "datatype", "effective_date", setk("effective_date", "20240501"))
case("Label", "future-effective-date", "pydantic_only:label-effective-date-not-future", "effective_date",
     setk("effective_date", "2999-01-01"), expect="declared_leak")
case("Label", "duplicate-section-code", "pydantic_only:label-section-codes-unique", "sections",
     lambda r: r["sections"].append({"code": "34067-9", "title": "INDICATIONS", "text": "Another text."}),
     note="Duplicate codes mint the same section IRI; the merged node breaks sh:maxCount, so SHACL rejects too.")
case("Label", "section-code-bad", "pattern", "sections.code", lambda r: r["sections"][0].__setitem__("code", "34067"))
case("Label", "section-text-missing", "node", "sections", lambda r: r["sections"][0].pop("text"))
case("Label", "product-ref-literal", "ref", "products", setk("products", ["243670"]))
case("AdverseEventReport", "serious-without-criterion", "rule:serious-requires-criterion", "seriousness_criteria", setk("seriousness_criteria", []))
case("AdverseEventReport", "no-suspect", "rule:has-suspect", "drugs",
     lambda r: r["drugs"][0].__setitem__("role", "interacting"))
case("AdverseEventReport", "source-system-missing", "required", "source_system", delk("source_system"))
case("AdverseEventReport", "source-system-unknown", "enum", "source_system", setk("source_system", "e2b"))
case("AdverseEventReport", "report-id-long-suffix", "pattern", "report_id", setk("report_id", "10003301-12345"))
case("AdverseEventReport", "report-id-prefixed", "pattern", "report_id", setk("report_id", "US-123"))
case("AdverseEventReport", "no-reactions", "min_count", "reactions", setk("reactions", []))
case("AdverseEventReport", "seven-criteria", "max_count", "seriousness_criteria",
     setk("seriousness_criteria", ["death", "life threatening", "hospitalization", "disabling", "congenital anomaly", "other", "unknown"]),
     note="The enum has six members, so a seventh criterion is also an enum violation.")
case("AdverseEventReport", "age-151", "max_inclusive", "patient_age_years", setk("patient_age_years", "151"))
case("AdverseEventReport", "age-negative", "min_inclusive", "patient_age_years", setk("patient_age_years", "-1"))
case("AdverseEventReport", "serious-as-string", "datatype", "serious", setk("serious", "true"))
case("AdverseEventReport", "unknown-role", "enum", "drugs.role", lambda r: r["drugs"][1].__setitem__("role", "suspect drug"))
case("AdverseEventReport", "reaction-as-string", "node", "reactions", setk("reactions", ["Nausea"]))
case("AdverseEventReport", "reaction-term-too-long", "max_length", "reactions.term", lambda r: r["reactions"][0].__setitem__("term", "N" * 101))
case("AdverseEventReport", "drug-clinical-drug-urn", "ref", "drugs.clinical_drug", lambda r: r["drugs"][0].__setitem__("clinical_drug", "urn:rxcui:243670"))
case("AdverseEventReport", "reaction-term-has-bell", "charset", "reactions.term", lambda r: r["reactions"][0].__setitem__("term", "Nausea\u0007"))
case("Recall", "recall-number-short", "pattern", "recall_number", setk("recall_number", "D-12-2025"))
case("Recall", "reason-has-nul", "charset", "reason", setk("reason", "Failed\u0000 dissolution."))
case("Recall", "classification-text", "enum", "classification", setk("classification", "Class II"))
case("Recall", "impossible-date", "datatype", "report_date", setk("report_date", "2025-02-30"))
case("Recall", "firm-missing", "required", "recalling_firm", delk("recalling_firm"))
case("Recall", "reason-empty", "min_length", "reason", setk("reason", ""))
case("Recall", "unknown-key", "closed", None, setk("status", "Ongoing"))

probes = [
  {"id": "trailing-newline-unii", "class": "Substance", "expect": "agree_invalid",
   "note": "pySHACL alone accepts '^...$' before a final newline. H6.1 closes it: sh:maxLength (implied by the fixed-length pattern) and the SafeIdentifier sh:not both reject.",
   "record": {"unii": "R16CO5Y76E\n", "name": "ASPIRIN"}},
  {"id": "trailing-newline-ndc", "class": "PackagedProduct", "expect": "agree_invalid", "note": "Closed the same way on an alternation pattern.",
   "record": dict(base("PackagedProduct"), ndc="0280-2000-10\n")},
  {"id": "trailing-newline-iri", "class": "PackagedProduct", "expect": "agree_invalid", "note": "The IRI pattern is unbounded, so only the sh:not on references closes it.",
   "record": dict(base("PackagedProduct"), labeler=org("bayer-healthcare-llc") + "\n")},
  {"id": "trailing-newline-slug", "class": "Organization", "expect": "agree_invalid", "note": "Unbounded pattern; closed by the SafeIdentifier sh:not alone.",
   "record": {"slug": "acme-pharma-inc\n", "name": "Acme"}},
  {"id": "inner-newline-unii", "class": "Substance", "expect": "agree_invalid", "note": "A newline before the end is rejected by every dialect.",
   "record": {"unii": "R16CO\n5Y76E", "name": "ASPIRIN"}},
  {"id": "unicode-digits-loinc", "class": "Label", "expect": "agree_invalid",
   "note": "Arabic-Indic digits match \\d in Rust, Python and XPath. The canonical [0-9] rejects them everywhere.",
   "record": dict(base("Label"), sections=[{"code": "\u0663\u0664\u0660\u0666\u0667-\u0669", "title": "INDICATIONS", "text": "x"}])},
]

def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
for cls, items in valid.items():
    dump(root / "fixtures/valid" / f"{cls}.json", [{"id": i, "record": r} for i, r in items])
for cls, items in invalid.items():
    dump(root / "fixtures/invalid" / f"{cls}.json", items)
dump(root / "fixtures/probes/dialect.json", probes)
print(sum(len(v) for v in valid.values()), sum(len(v) for v in invalid.values()), len(probes))
