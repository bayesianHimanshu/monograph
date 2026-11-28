-- MONOGRAPH drug product model 2.2.0, default profile. Generated from core/models.py; do not edit.
-- Run inside an empty schema. Constraint names are stable and used by the integration test.

CREATE TABLE substance (
    iri text PRIMARY KEY,
    unii text NOT NULL,
    name text NOT NULL,
    CONSTRAINT ck_substance_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_substance_unii_charset CHECK (unii !~ '[\x01-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_substance_unii_pattern CHECK (unii ~ '^[0-9A-Z]{10}$'),
    CONSTRAINT ck_substance_name_charset CHECK (name !~ '[\x01-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_substance_name_min_length CHECK (char_length(name) >= 1),
    CONSTRAINT ck_substance_name_max_length CHECK (char_length(name) <= 500),
    CONSTRAINT uq_substance_key UNIQUE (unii)
);
COMMENT ON TABLE substance IS 'A chemical or biological substance identified by its FDA Unique Ingredient Identifier.';
COMMENT ON COLUMN substance.iri IS 'Canonical identifier (IRI).';
COMMENT ON COLUMN substance.unii IS 'FDA UNII code.';
COMMENT ON COLUMN substance.name IS 'Preferred name.';

CREATE TABLE organization (
    iri text PRIMARY KEY,
    slug text NOT NULL,
    name text NOT NULL,
    CONSTRAINT ck_organization_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_organization_slug_charset CHECK (slug !~ '[\x01-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_organization_slug_pattern CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    CONSTRAINT ck_organization_slug_max_length CHECK (char_length(slug) <= 120),
    CONSTRAINT ck_organization_name_charset CHECK (name !~ '[\x01-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_organization_name_min_length CHECK (char_length(name) >= 1),
    CONSTRAINT ck_organization_name_max_length CHECK (char_length(name) <= 500),
    CONSTRAINT uq_organization_key UNIQUE (slug)
);
COMMENT ON TABLE organization IS 'A company acting as manufacturer, labeler or recalling firm.';
COMMENT ON COLUMN organization.iri IS 'Canonical identifier (IRI).';
COMMENT ON COLUMN organization.slug IS 'Stable key: the normalized name, lowercase, words joined by hyphens.';
COMMENT ON COLUMN organization.name IS 'Preferred name.';

CREATE TABLE clinical_drug (
    iri text PRIMARY KEY,
    rxcui text NOT NULL,
    name text,
    term_type text,
    CONSTRAINT ck_clinical_drug_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_clinical_drug_rxcui_charset CHECK (rxcui !~ '[\x01-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_clinical_drug_rxcui_pattern CHECK (rxcui ~ '^[0-9]{1,8}$'),
    CONSTRAINT ck_clinical_drug_name_charset CHECK (name !~ '[\x01-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_clinical_drug_name_min_length CHECK (char_length(name) >= 1),
    CONSTRAINT ck_clinical_drug_name_max_length CHECK (char_length(name) <= 500),
    CONSTRAINT ck_clinical_drug_term_type_charset CHECK (term_type !~ '[\x01-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_clinical_drug_term_type_pattern CHECK (term_type ~ '^[A-Z]{2,6}$'),
    CONSTRAINT uq_clinical_drug_key UNIQUE (rxcui)
);
COMMENT ON TABLE clinical_drug IS 'A clinical drug concept in RxNorm, identified by its RxCUI.

    Several NDC products can share one clinical drug. The name is optional because openFDA carries
    RxCUIs without their RxNorm names.';
COMMENT ON COLUMN clinical_drug.iri IS 'Canonical identifier (IRI).';
COMMENT ON COLUMN clinical_drug.rxcui IS 'RxNorm CUI.';
COMMENT ON COLUMN clinical_drug.name IS 'RxNorm name.';
COMMENT ON COLUMN clinical_drug.term_type IS 'RxNorm term type: SCD generic clinical drug, SBD branded drug, GPCK/BPCK packs.';

CREATE TABLE product (
    iri text PRIMARY KEY,
    product_ndc text NOT NULL,
    name text NOT NULL,
    dosage_form text NOT NULL,
    product_type text NOT NULL,
    finished boolean NOT NULL,
    routes text[] NOT NULL DEFAULT '{}',
    CONSTRAINT ck_product_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_product_product_ndc_charset CHECK (product_ndc !~ '[\x01-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_product_product_ndc_pattern CHECK (product_ndc ~ '^([0-9]{4}-[0-9]{4}|[0-9]{5}-[0-9]{3}|[0-9]{5}-[0-9]{4})$'),
    CONSTRAINT ck_product_name_charset CHECK (name !~ '[\x01-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_product_name_min_length CHECK (char_length(name) >= 1),
    CONSTRAINT ck_product_name_max_length CHECK (char_length(name) <= 500),
    CONSTRAINT ck_product_dosage_form_charset CHECK (dosage_form !~ '[\x01-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_product_dosage_form_min_length CHECK (char_length(dosage_form) >= 1),
    CONSTRAINT ck_product_dosage_form_max_length CHECK (char_length(dosage_form) <= 100),
    CONSTRAINT ck_product_product_type_enum CHECK (product_type IN ('HUMAN PRESCRIPTION DRUG', 'HUMAN OTC DRUG', 'PLASMA DERIVATIVE', 'CELLULAR THERAPY', 'VACCINE', 'STANDARDIZED ALLERGENIC', 'NON-STANDARDIZED ALLERGENIC', 'BULK INGREDIENT', 'DRUG FOR FURTHER PROCESSING', 'LICENSED VACCINE BULK INTERMEDIATE')),
    CONSTRAINT ck_product_routes_enum CHECK (routes <@ ARRAY['AURICULAR (OTIC)', 'BUCCAL', 'CONJUNCTIVAL', 'CUTANEOUS', 'DENTAL', 'ELECTRO-OSMOSIS', 'ENDOCERVICAL', 'ENDOSINUSIAL', 'ENDOTRACHEAL', 'ENTERAL', 'EPIDURAL', 'EXTRA-AMNIOTIC', 'EXTRACORPOREAL', 'HEMODIALYSIS', 'INFILTRATION', 'INTERSTITIAL', 'INTRA-ABDOMINAL', 'INTRA-AMNIOTIC', 'INTRA-ARTERIAL', 'INTRA-ARTICULAR', 'INTRABILIARY', 'INTRABRONCHIAL', 'INTRABURSAL', 'INTRACAMERAL', 'INTRACANALICULAR', 'INTRACARDIAC', 'INTRACARTILAGINOUS', 'INTRACAUDAL', 'INTRACAVERNOUS', 'INTRACAVITARY', 'INTRACEREBRAL', 'INTRACISTERNAL', 'INTRACORNEAL', 'INTRACORONAL, DENTAL', 'INTRACORONARY', 'INTRACORPORUS CAVERNOSUM', 'INTRACRANIAL', 'INTRADERMAL', 'INTRADISCAL', 'INTRADUCTAL', 'INTRADUODENAL', 'INTRADURAL', 'INTRAEPICARDIAL', 'INTRAEPIDERMAL', 'INTRAESOPHAGEAL', 'INTRAGASTRIC', 'INTRAGINGIVAL', 'INTRAHEPATIC', 'INTRAILEAL', 'INTRALESIONAL', 'INTRALINGUAL', 'INTRALUMINAL', 'INTRALYMPHATIC', 'INTRAMAMMARY', 'INTRAMEDULLARY', 'INTRAMENINGEAL', 'INTRAMUSCULAR', 'INTRANODAL', 'INTRAOCULAR', 'INTRAOMENTUM', 'INTRAOVARIAN', 'INTRAPERICARDIAL', 'INTRAPERITONEAL', 'INTRAPLEURAL', 'INTRAPROSTATIC', 'INTRAPULMONARY', 'INTRARUMINAL', 'INTRASINAL', 'INTRASPINAL', 'INTRASYNOVIAL', 'INTRATENDINOUS', 'INTRATESTICULAR', 'INTRATHECAL', 'INTRATHORACIC', 'INTRATUBULAR', 'INTRATUMOR', 'INTRATYMPANIC', 'INTRAUTERINE', 'INTRAVASCULAR', 'INTRAVENOUS', 'INTRAVENTRICULAR', 'INTRAVESICAL', 'INTRAVITREAL', 'IONTOPHORESIS', 'IRRIGATION', 'LARYNGEAL', 'NASAL', 'NASOGASTRIC', 'NOT APPLICABLE', 'OCCLUSIVE DRESSING TECHNIQUE', 'OPHTHALMIC', 'ORAL', 'OROPHARYNGEAL', 'PARENTERAL', 'PERCUTANEOUS', 'PERIARTICULAR', 'PERIDURAL', 'PERINEURAL', 'PERIODONTAL', 'RECTAL', 'RESPIRATORY (INHALATION)', 'RETROBULBAR', 'SOFT TISSUE', 'SUBARACHNOID', 'SUBCONJUNCTIVAL', 'SUBCUTANEOUS', 'SUBGINGIVAL', 'SUBLINGUAL', 'SUBMUCOSAL', 'SUBRETINAL', 'SUPRACHOROIDAL', 'TOPICAL', 'TRANSDERMAL', 'TRANSENDOCARDIAL', 'TRANSMUCOSAL', 'TRANSPLACENTAL', 'TRANSTRACHEAL', 'TRANSTYMPANIC', 'URETERAL', 'URETHRAL', 'VAGINAL']::text[]),
    CONSTRAINT uq_product_key UNIQUE (product_ndc),
    CONSTRAINT ck_product_rule_finished_rx_otc_requires_route CHECK (finished IS NULL OR finished NOT IN (true) OR product_type IS NULL OR product_type NOT IN ('HUMAN PRESCRIPTION DRUG', 'HUMAN OTC DRUG') OR cardinality(routes) >= 1)
);
COMMENT ON TABLE product IS 'A drug product as listed with FDA, identified by its product NDC (labeler and product segments).';
COMMENT ON COLUMN product.iri IS 'Canonical identifier (IRI).';
COMMENT ON COLUMN product.product_ndc IS 'Product NDC in 4-4, 5-3 or 5-4 form.';
COMMENT ON COLUMN product.name IS 'Name as listed.';
COMMENT ON COLUMN product.dosage_form IS 'Dosage form as written in the source.';
COMMENT ON COLUMN product.product_type IS 'NDC directory product type. One of: HUMAN PRESCRIPTION DRUG, HUMAN OTC DRUG, PLASMA DERIVATIVE, CELLULAR THERAPY, VACCINE, STANDARDIZED ALLERGENIC, NON-STANDARDIZED ALLERGENIC, BULK INGREDIENT, DRUG FOR FURTHER PROCESSING, LICENSED VACCINE BULK INTERMEDIATE.';
COMMENT ON COLUMN product.finished IS 'Whether this is a finished product (False for bulk ingredients and intermediates).';
COMMENT ON COLUMN product.routes IS 'Routes of administration. Required for finished prescription and OTC products.';

CREATE TABLE product__ingredients (
    iri text PRIMARY KEY,
    owner_iri text NOT NULL REFERENCES product (iri) ON DELETE CASCADE,
    position integer NOT NULL,
    substance_iri text NOT NULL REFERENCES substance (iri),
    strength_value numeric NOT NULL,
    strength_unit text NOT NULL,
    CONSTRAINT ck_product__ingredients_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_product__ingredients_position_min_inclusive CHECK (position >= 0),
    CONSTRAINT uq_product__ingredients_position UNIQUE (owner_iri, position),
    CONSTRAINT ck_product__ingredients_substance_iri_pattern CHECK (substance_iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_product__ingredients_strength_value_finite CHECK (strength_value NOT IN ('NaN', 'Infinity', '-Infinity')),
    CONSTRAINT ck_product__ingredients_strength_value_min_exclusive CHECK (strength_value > 0),
    CONSTRAINT ck_product__ingredients_strength_unit_charset CHECK (strength_unit !~ '[\x01-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_product__ingredients_strength_unit_min_length CHECK (char_length(strength_unit) >= 1),
    CONSTRAINT ck_product__ingredients_strength_unit_max_length CHECK (char_length(strength_unit) <= 50),
    CONSTRAINT uq_product__ingredients_substance UNIQUE (owner_iri, substance_iri)
);
COMMENT ON TABLE product__ingredients IS 'Product.ingredients: Active ingredients, in order. An active ingredient of a product with its strength.';
COMMENT ON COLUMN product__ingredients.iri IS 'Canonical identifier (IRI).';
COMMENT ON COLUMN product__ingredients.owner_iri IS 'The owning Product row (product.iri).';
COMMENT ON COLUMN product__ingredients.position IS 'Position in the owner''s list, from 0.';
COMMENT ON COLUMN product__ingredients.substance_iri IS 'The substance.';
COMMENT ON COLUMN product__ingredients.strength_value IS 'Numeric strength.';
COMMENT ON COLUMN product__ingredients.strength_unit IS 'Unit of the strength, for example mg/1.';

CREATE TABLE packaged_product (
    iri text PRIMARY KEY,
    ndc text NOT NULL,
    product_iri text NOT NULL REFERENCES product (iri),
    labeler_iri text NOT NULL REFERENCES organization (iri),
    description text,
    CONSTRAINT ck_packaged_product_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_packaged_product_ndc_charset CHECK (ndc !~ '[\x01-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_packaged_product_ndc_pattern CHECK (ndc ~ '^([0-9]{4}-[0-9]{4}-[0-9]{2}|[0-9]{5}-[0-9]{3}-[0-9]{2}|[0-9]{5}-[0-9]{4}-[0-9])$'),
    CONSTRAINT ck_packaged_product_product_iri_pattern CHECK (product_iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_packaged_product_labeler_iri_pattern CHECK (labeler_iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_packaged_product_description_charset CHECK (description !~ '[\x01-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_packaged_product_description_min_length CHECK (char_length(description) >= 1),
    CONSTRAINT ck_packaged_product_description_max_length CHECK (char_length(description) <= 2000),
    CONSTRAINT uq_packaged_product_key UNIQUE (ndc)
);
COMMENT ON TABLE packaged_product IS 'A package of a product, identified by its 10-digit hyphenated package NDC.';
COMMENT ON COLUMN packaged_product.iri IS 'Canonical identifier (IRI).';
COMMENT ON COLUMN packaged_product.ndc IS 'Package NDC in 4-4-2, 5-3-2 or 5-4-1 form.';
COMMENT ON COLUMN packaged_product.product_iri IS 'The packaged product.';
COMMENT ON COLUMN packaged_product.labeler_iri IS 'The labeler firm.';
COMMENT ON COLUMN packaged_product.description IS 'Package description as written in the source.';

CREATE TABLE label (
    iri text PRIMARY KEY,
    set_id text NOT NULL,
    version bigint NOT NULL,
    effective_date date NOT NULL,
    CONSTRAINT ck_label_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_label_set_id_charset CHECK (set_id !~ '[\x01-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_label_set_id_pattern CHECK (set_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    CONSTRAINT ck_label_version_min_inclusive CHECK (version >= 1),
    CONSTRAINT ck_label_version_max_exclusive CHECK (version < 1000000),
    CONSTRAINT uq_label_key UNIQUE (set_id, version)
);
COMMENT ON TABLE label IS 'One version of a structured product label (SPL).';
COMMENT ON COLUMN label.iri IS 'Canonical identifier (IRI).';
COMMENT ON COLUMN label.set_id IS 'SPL set ID, a lowercase UUID.';
COMMENT ON COLUMN label.version IS 'SPL version.';
COMMENT ON COLUMN label.effective_date IS 'SPL effective time.';

CREATE TABLE label__products (
    owner_iri text NOT NULL REFERENCES label (iri) ON DELETE CASCADE,
    target_iri text NOT NULL REFERENCES product (iri),
    CONSTRAINT ck_label__products_target_iri_pattern CHECK (target_iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT pk_label__products PRIMARY KEY (owner_iri, target_iri)
);
COMMENT ON TABLE label__products IS 'Label.products: Products.';
COMMENT ON COLUMN label__products.owner_iri IS 'The Label row (label.iri).';
COMMENT ON COLUMN label__products.target_iri IS 'The Product row (product.iri).';

CREATE TABLE label__sections (
    iri text PRIMARY KEY,
    owner_iri text NOT NULL REFERENCES label (iri) ON DELETE CASCADE,
    code text NOT NULL,
    title text NOT NULL,
    text text NOT NULL,
    CONSTRAINT ck_label__sections_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT uq_label__sections_key UNIQUE (owner_iri, code),
    CONSTRAINT ck_label__sections_code_charset CHECK (code !~ '[\x01-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_label__sections_code_pattern CHECK (code ~ '^[0-9]{1,7}-[0-9]$'),
    CONSTRAINT ck_label__sections_title_charset CHECK (title !~ '[\x01-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_label__sections_title_min_length CHECK (char_length(title) >= 1),
    CONSTRAINT ck_label__sections_title_max_length CHECK (char_length(title) <= 200),
    CONSTRAINT ck_label__sections_text_charset CHECK (text !~ '[\x01-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_label__sections_text_min_length CHECK (char_length(text) >= 1)
);
COMMENT ON TABLE label__sections IS 'Label.sections: Coded sections. One LOINC-coded section of a structured product label.';
COMMENT ON COLUMN label__sections.iri IS 'Canonical identifier (IRI).';
COMMENT ON COLUMN label__sections.owner_iri IS 'The owning Label row (label.iri).';
COMMENT ON COLUMN label__sections.code IS 'LOINC code.';
COMMENT ON COLUMN label__sections.title IS 'Title.';
COMMENT ON COLUMN label__sections.text IS 'Plain text.';

CREATE TABLE adverse_event_report (
    iri text PRIMARY KEY,
    report_id text NOT NULL,
    source_system text NOT NULL,
    receipt_date date NOT NULL,
    serious boolean NOT NULL,
    seriousness_criteria text[] NOT NULL DEFAULT '{}',
    patient_age_years numeric,
    CONSTRAINT ck_adverse_event_report_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_adverse_event_report_report_id_charset CHECK (report_id !~ '[\x01-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_adverse_event_report_report_id_pattern CHECK (report_id ~ '^[0-9]{1,12}(-[0-9]{1,3})?$'),
    CONSTRAINT ck_adverse_event_report_source_system_enum CHECK (source_system IN ('openfda_e2b', 'faers_qde')),
    CONSTRAINT ck_adverse_event_report_seriousness_criteria_enum CHECK (seriousness_criteria <@ ARRAY['death', 'life threatening', 'hospitalization', 'disabling', 'congenital anomaly', 'other']::text[]),
    CONSTRAINT ck_adverse_event_report_seriousness_criteria_max_count CHECK (cardinality(seriousness_criteria) <= 6),
    CONSTRAINT ck_adverse_event_report_patient_age_years_finite CHECK (patient_age_years NOT IN ('NaN', 'Infinity', '-Infinity')),
    CONSTRAINT ck_adverse_event_report_patient_age_years_min_inclusive CHECK (patient_age_years >= 0),
    CONSTRAINT ck_adverse_event_report_patient_age_years_max_inclusive CHECK (patient_age_years <= 150),
    CONSTRAINT uq_adverse_event_report_key UNIQUE (report_id),
    CONSTRAINT ck_adverse_event_report_rule_serious_requires_criterion CHECK (serious IS NULL OR serious NOT IN (true) OR cardinality(seriousness_criteria) >= 1)
);
COMMENT ON TABLE adverse_event_report IS 'A safety report from FDA adverse event data (FAERS, now FDA AEMS), via openFDA.';
COMMENT ON COLUMN adverse_event_report.iri IS 'Canonical identifier (IRI).';
COMMENT ON COLUMN adverse_event_report.report_id IS 'Safety report ID, optionally with a -digits version suffix.';
COMMENT ON COLUMN adverse_event_report.source_system IS 'Where the report came from. One of: openfda_e2b, faers_qde.';
COMMENT ON COLUMN adverse_event_report.receipt_date IS 'Date FDA received the report.';
COMMENT ON COLUMN adverse_event_report.serious IS 'Whether the report is serious.';
COMMENT ON COLUMN adverse_event_report.seriousness_criteria IS 'Seriousness criteria met. One of: death, life threatening, hospitalization, disabling, congenital anomaly, other.';
COMMENT ON COLUMN adverse_event_report.patient_age_years IS 'Patient age at onset, in years.';

CREATE TABLE adverse_event_report__drugs (
    iri text PRIMARY KEY,
    owner_iri text NOT NULL REFERENCES adverse_event_report (iri) ON DELETE CASCADE,
    position integer NOT NULL,
    drug_name text NOT NULL,
    role text NOT NULL,
    CONSTRAINT ck_adverse_event_report__drugs_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_adverse_event_report__drugs_position_min_inclusive CHECK (position >= 0),
    CONSTRAINT uq_adverse_event_report__drugs_position UNIQUE (owner_iri, position),
    CONSTRAINT ck_adverse_event_report__drugs_drug_name_charset CHECK (drug_name !~ '[\x01-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_adverse_event_report__drugs_drug_name_min_length CHECK (char_length(drug_name) >= 1),
    CONSTRAINT ck_adverse_event_report__drugs_drug_name_max_length CHECK (char_length(drug_name) <= 500),
    CONSTRAINT ck_adverse_event_report__drugs_role_enum CHECK (role IN ('primary suspect', 'secondary suspect', 'suspect', 'concomitant', 'interacting'))
);
COMMENT ON TABLE adverse_event_report__drugs IS 'AdverseEventReport.drugs: Drugs, in reported order. A drug mentioned in an adverse event report, as reported.';
COMMENT ON COLUMN adverse_event_report__drugs.iri IS 'Canonical identifier (IRI).';
COMMENT ON COLUMN adverse_event_report__drugs.owner_iri IS 'The owning AdverseEventReport row (adverse_event_report.iri).';
COMMENT ON COLUMN adverse_event_report__drugs.position IS 'Position in the owner''s list, from 0.';
COMMENT ON COLUMN adverse_event_report__drugs.drug_name IS 'Drug name exactly as reported.';
COMMENT ON COLUMN adverse_event_report__drugs.role IS 'Role in the report. One of: primary suspect, secondary suspect, suspect, concomitant, interacting.';

CREATE TABLE adverse_event_report__reactions (
    iri text PRIMARY KEY,
    owner_iri text NOT NULL REFERENCES adverse_event_report (iri) ON DELETE CASCADE,
    position integer NOT NULL,
    term text NOT NULL,
    CONSTRAINT ck_adverse_event_report__reactions_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_adverse_event_report__reactions_position_min_inclusive CHECK (position >= 0),
    CONSTRAINT uq_adverse_event_report__reactions_position UNIQUE (owner_iri, position),
    CONSTRAINT ck_adverse_event_report__reactions_term_charset CHECK (term !~ '[\x01-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_adverse_event_report__reactions_term_min_length CHECK (char_length(term) >= 1),
    CONSTRAINT ck_adverse_event_report__reactions_term_max_length CHECK (char_length(term) <= 100)
);
COMMENT ON TABLE adverse_event_report__reactions IS 'AdverseEventReport.reactions: Reactions, in order. A reaction, as a MedDRA preferred term string. No MedDRA hierarchy is shipped (licensed).';
COMMENT ON COLUMN adverse_event_report__reactions.iri IS 'Canonical identifier (IRI).';
COMMENT ON COLUMN adverse_event_report__reactions.owner_iri IS 'The owning AdverseEventReport row (adverse_event_report.iri).';
COMMENT ON COLUMN adverse_event_report__reactions.position IS 'Position in the owner''s list, from 0.';
COMMENT ON COLUMN adverse_event_report__reactions.term IS 'MedDRA PT text (at most 100 characters).';

CREATE TABLE recall (
    iri text PRIMARY KEY,
    recall_number text NOT NULL,
    classification text NOT NULL,
    reason text NOT NULL,
    report_date date NOT NULL,
    recalling_firm_iri text NOT NULL REFERENCES organization (iri),
    CONSTRAINT ck_recall_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_recall_recall_number_charset CHECK (recall_number !~ '[\x01-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_recall_recall_number_pattern CHECK (recall_number ~ '^D-[0-9]{3,4}-[0-9]{4}$'),
    CONSTRAINT ck_recall_classification_enum CHECK (classification IN ('I', 'II', 'III')),
    CONSTRAINT ck_recall_reason_charset CHECK (reason !~ '[\x01-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_recall_reason_min_length CHECK (char_length(reason) >= 1),
    CONSTRAINT ck_recall_recalling_firm_iri_pattern CHECK (recalling_firm_iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT uq_recall_key UNIQUE (recall_number)
);
COMMENT ON TABLE recall IS 'A drug recall from the FDA enforcement report.';
COMMENT ON COLUMN recall.iri IS 'Canonical identifier (IRI).';
COMMENT ON COLUMN recall.recall_number IS 'FDA recall number, for example D-321-2016 or D-0123-2025.';
COMMENT ON COLUMN recall.classification IS 'Class. One of: I, II, III.';
COMMENT ON COLUMN recall.reason IS 'Reason for the recall.';
COMMENT ON COLUMN recall.report_date IS 'Enforcement report date.';
COMMENT ON COLUMN recall.recalling_firm_iri IS 'The recalling firm.';

CREATE TABLE recall__products (
    owner_iri text NOT NULL REFERENCES recall (iri) ON DELETE CASCADE,
    target_iri text NOT NULL REFERENCES product (iri),
    CONSTRAINT ck_recall__products_target_iri_pattern CHECK (target_iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT pk_recall__products PRIMARY KEY (owner_iri, target_iri)
);
COMMENT ON TABLE recall__products IS 'Recall.products: Products.';
COMMENT ON COLUMN recall__products.owner_iri IS 'The Recall row (recall.iri).';
COMMENT ON COLUMN recall__products.target_iri IS 'The Product row (product.iri).';

CREATE TABLE label_extraction (
    iri text PRIMARY KEY,
    set_id text NOT NULL,
    version bigint NOT NULL,
    CONSTRAINT ck_label_extraction_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_label_extraction_set_id_charset CHECK (set_id !~ '[\x01-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_label_extraction_set_id_pattern CHECK (set_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    CONSTRAINT ck_label_extraction_version_min_inclusive CHECK (version >= 1),
    CONSTRAINT ck_label_extraction_version_max_exclusive CHECK (version < 1000000),
    CONSTRAINT uq_label_extraction_key UNIQUE (set_id, version)
);
COMMENT ON TABLE label_extraction IS 'What an extractor read from one label''s narrative, plus the label it came from.';
COMMENT ON COLUMN label_extraction.iri IS 'Canonical identifier (IRI).';
COMMENT ON COLUMN label_extraction.set_id IS 'SPL set ID of the label read.';
COMMENT ON COLUMN label_extraction.version IS 'SPL version read.';

CREATE TABLE label_extraction__products (
    iri text PRIMARY KEY,
    owner_iri text NOT NULL REFERENCES label_extraction (iri) ON DELETE CASCADE,
    position integer NOT NULL,
    name text NOT NULL,
    dosage_form text NOT NULL,
    routes text[] NOT NULL DEFAULT '{}',
    CONSTRAINT ck_label_extraction__products_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_label_extraction__products_position_min_inclusive CHECK (position >= 0),
    CONSTRAINT uq_label_extraction__products_position UNIQUE (owner_iri, position),
    CONSTRAINT ck_label_extraction__products_name_charset CHECK (name !~ '[\x01-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_label_extraction__products_name_min_length CHECK (char_length(name) >= 1),
    CONSTRAINT ck_label_extraction__products_name_max_length CHECK (char_length(name) <= 500),
    CONSTRAINT ck_label_extraction__products_dosage_form_enum CHECK (dosage_form IN ('AEROSOL', 'AEROSOL, FOAM', 'AEROSOL, METERED', 'AEROSOL, POWDER', 'AEROSOL, SPRAY', 'BAR, CHEWABLE', 'BEAD', 'CAPSULE', 'CAPSULE, COATED', 'CAPSULE, COATED PELLETS', 'CAPSULE, COATED, EXTENDED RELEASE', 'CAPSULE, DELAYED RELEASE', 'CAPSULE, DELAYED RELEASE PELLETS', 'CAPSULE, EXTENDED RELEASE', 'CAPSULE, FILM COATED, EXTENDED RELEASE', 'CAPSULE, GELATIN COATED', 'CAPSULE, LIQUID FILLED', 'CELLULAR SHEET', 'CHEWABLE GEL', 'CLOTH', 'CONCENTRATE', 'CREAM', 'CREAM, AUGMENTED', 'CRYSTAL', 'DISC', 'DOUCHE', 'DRESSING', 'DRUG-ELUTING CONTACT LENS', 'ELIXIR', 'EMULSION', 'ENEMA', 'EXTRACT', 'FIBER, EXTENDED RELEASE', 'FILM', 'FILM, EXTENDED RELEASE', 'FILM, SOLUBLE', 'FOR SOLUTION', 'FOR SUSPENSION', 'FOR SUSPENSION, EXTENDED RELEASE', 'GAS', 'GEL', 'GEL, DENTIFRICE', 'GEL, METERED', 'GLOBULE', 'GRANULE', 'GRANULE, DELAYED RELEASE', 'GRANULE, EFFERVESCENT', 'GRANULE, FOR SOLUTION', 'GRANULE, FOR SUSPENSION', 'GRANULE, FOR SUSPENSION, EXTENDED RELEASE', 'GUM, CHEWING', 'IMPLANT', 'INHALANT', 'INJECTABLE FOAM', 'INJECTABLE, LIPOSOMAL', 'INJECTION', 'INJECTION, EMULSION', 'INJECTION, LIPID COMPLEX', 'INJECTION, POWDER, FOR SOLUTION', 'INJECTION, POWDER, FOR SUSPENSION', 'INJECTION, POWDER, FOR SUSPENSION, EXTENDED RELEASE', 'INJECTION, POWDER, LYOPHILIZED, FOR LIPOSOMAL SUSPENSION', 'INJECTION, POWDER, LYOPHILIZED, FOR SOLUTION', 'INJECTION, POWDER, LYOPHILIZED, FOR SUSPENSION', 'INJECTION, POWDER, LYOPHILIZED, FOR SUSPENSION, EXTENDED RELEASE', 'INJECTION, SOLUTION', 'INJECTION, SOLUTION, CONCENTRATE', 'INJECTION, SUSPENSION', 'INJECTION, SUSPENSION, EXTENDED RELEASE', 'INJECTION, SUSPENSION, LIPOSOMAL', 'INJECTION, SUSPENSION, SONICATED', 'INSERT', 'INSERT, EXTENDED RELEASE', 'INTRAUTERINE DEVICE', 'IRRIGANT', 'JELLY', 'KIT', 'LINIMENT', 'LIPSTICK', 'LIQUID', 'LIQUID, EXTENDED RELEASE', 'LOTION', 'LOTION, AUGMENTED', 'LOTION/SHAMPOO', 'LOZENGE', 'MOUTHWASH', 'NOT APPLICABLE', 'OIL', 'OINTMENT', 'OINTMENT, AUGMENTED', 'PASTE', 'PASTE, DENTIFRICE', 'PASTILLE', 'PATCH', 'PATCH, EXTENDED RELEASE', 'PATCH, EXTENDED RELEASE, ELECTRICALLY CONTROLLED', 'PELLET', 'PELLET, IMPLANTABLE', 'PELLETS, COATED, EXTENDED RELEASE', 'PILL', 'PLASTER', 'POULTICE', 'POWDER', 'POWDER, DENTIFRICE', 'POWDER, FOR SOLUTION', 'POWDER, FOR SUSPENSION', 'POWDER, METERED', 'RING', 'RINSE', 'SALVE', 'SHAMPOO', 'SHAMPOO, SUSPENSION', 'SOAP', 'SOLUTION', 'SOLUTION, CONCENTRATE', 'SOLUTION, FOR SLUSH', 'SOLUTION, GEL FORMING / DROPS', 'SOLUTION, GEL FORMING, EXTENDED RELEASE', 'SOLUTION/ DROPS', 'SPONGE', 'SPRAY', 'SPRAY, METERED', 'SPRAY, SUSPENSION', 'STICK', 'STRIP', 'SUPPOSITORY', 'SUPPOSITORY, EXTENDED RELEASE', 'SUSPENSION', 'SUSPENSION, EXTENDED RELEASE', 'SUSPENSION/ DROPS', 'SWAB', 'SYRUP', 'SYSTEM', 'TABLET', 'TABLET, CHEWABLE', 'TABLET, CHEWABLE, EXTENDED RELEASE', 'TABLET, COATED', 'TABLET, COATED PARTICLES', 'TABLET, DELAYED RELEASE', 'TABLET, DELAYED RELEASE PARTICLES', 'TABLET, EFFERVESCENT', 'TABLET, EXTENDED RELEASE', 'TABLET, FILM COATED', 'TABLET, FILM COATED, EXTENDED RELEASE', 'TABLET, FOR SOLUTION', 'TABLET, FOR SUSPENSION', 'TABLET, MULTILAYER', 'TABLET, MULTILAYER, EXTENDED RELEASE', 'TABLET, ORALLY DISINTEGRATING', 'TABLET, ORALLY DISINTEGRATING, DELAYED RELEASE', 'TABLET, SOLUBLE', 'TABLET, SUGAR COATED', 'TABLET WITH SENSOR', 'TAMPON', 'TAPE', 'TINCTURE', 'TROCHE', 'WAFER')),
    CONSTRAINT ck_label_extraction__products_routes_enum CHECK (routes <@ ARRAY['AURICULAR (OTIC)', 'BUCCAL', 'CONJUNCTIVAL', 'CUTANEOUS', 'DENTAL', 'ELECTRO-OSMOSIS', 'ENDOCERVICAL', 'ENDOSINUSIAL', 'ENDOTRACHEAL', 'ENTERAL', 'EPIDURAL', 'EXTRA-AMNIOTIC', 'EXTRACORPOREAL', 'HEMODIALYSIS', 'INFILTRATION', 'INTERSTITIAL', 'INTRA-ABDOMINAL', 'INTRA-AMNIOTIC', 'INTRA-ARTERIAL', 'INTRA-ARTICULAR', 'INTRABILIARY', 'INTRABRONCHIAL', 'INTRABURSAL', 'INTRACAMERAL', 'INTRACANALICULAR', 'INTRACARDIAC', 'INTRACARTILAGINOUS', 'INTRACAUDAL', 'INTRACAVERNOUS', 'INTRACAVITARY', 'INTRACEREBRAL', 'INTRACISTERNAL', 'INTRACORNEAL', 'INTRACORONAL, DENTAL', 'INTRACORONARY', 'INTRACORPORUS CAVERNOSUM', 'INTRACRANIAL', 'INTRADERMAL', 'INTRADISCAL', 'INTRADUCTAL', 'INTRADUODENAL', 'INTRADURAL', 'INTRAEPICARDIAL', 'INTRAEPIDERMAL', 'INTRAESOPHAGEAL', 'INTRAGASTRIC', 'INTRAGINGIVAL', 'INTRAHEPATIC', 'INTRAILEAL', 'INTRALESIONAL', 'INTRALINGUAL', 'INTRALUMINAL', 'INTRALYMPHATIC', 'INTRAMAMMARY', 'INTRAMEDULLARY', 'INTRAMENINGEAL', 'INTRAMUSCULAR', 'INTRANODAL', 'INTRAOCULAR', 'INTRAOMENTUM', 'INTRAOVARIAN', 'INTRAPERICARDIAL', 'INTRAPERITONEAL', 'INTRAPLEURAL', 'INTRAPROSTATIC', 'INTRAPULMONARY', 'INTRARUMINAL', 'INTRASINAL', 'INTRASPINAL', 'INTRASYNOVIAL', 'INTRATENDINOUS', 'INTRATESTICULAR', 'INTRATHECAL', 'INTRATHORACIC', 'INTRATUBULAR', 'INTRATUMOR', 'INTRATYMPANIC', 'INTRAUTERINE', 'INTRAVASCULAR', 'INTRAVENOUS', 'INTRAVENTRICULAR', 'INTRAVESICAL', 'INTRAVITREAL', 'IONTOPHORESIS', 'IRRIGATION', 'LARYNGEAL', 'NASAL', 'NASOGASTRIC', 'NOT APPLICABLE', 'OCCLUSIVE DRESSING TECHNIQUE', 'OPHTHALMIC', 'ORAL', 'OROPHARYNGEAL', 'PARENTERAL', 'PERCUTANEOUS', 'PERIARTICULAR', 'PERIDURAL', 'PERINEURAL', 'PERIODONTAL', 'RECTAL', 'RESPIRATORY (INHALATION)', 'RETROBULBAR', 'SOFT TISSUE', 'SUBARACHNOID', 'SUBCONJUNCTIVAL', 'SUBCUTANEOUS', 'SUBGINGIVAL', 'SUBLINGUAL', 'SUBMUCOSAL', 'SUBRETINAL', 'SUPRACHOROIDAL', 'TOPICAL', 'TRANSDERMAL', 'TRANSENDOCARDIAL', 'TRANSMUCOSAL', 'TRANSPLACENTAL', 'TRANSTRACHEAL', 'TRANSTYMPANIC', 'URETERAL', 'URETHRAL', 'VAGINAL']::text[]),
    CONSTRAINT ck_label_extraction__products_routes_min_count CHECK (cardinality(routes) >= 1)
);
COMMENT ON TABLE label_extraction__products IS 'LabelExtraction.products: Products the label describes. One product described by a label: what it is made of, its form, its routes, its packages.';
COMMENT ON COLUMN label_extraction__products.iri IS 'Canonical identifier (IRI).';
COMMENT ON COLUMN label_extraction__products.owner_iri IS 'The owning LabelExtraction row (label_extraction.iri).';
COMMENT ON COLUMN label_extraction__products.position IS 'Position in the owner''s list, from 0.';
COMMENT ON COLUMN label_extraction__products.name IS 'Product name.';
COMMENT ON COLUMN label_extraction__products.dosage_form IS 'Dosage form.';
COMMENT ON COLUMN label_extraction__products.routes IS 'Routes of administration.';

CREATE TABLE label_extraction__products__ingredients (
    iri text PRIMARY KEY,
    owner_iri text NOT NULL REFERENCES label_extraction__products (iri) ON DELETE CASCADE,
    position integer NOT NULL,
    substance_name text NOT NULL,
    strength_value numeric NOT NULL,
    strength_unit text NOT NULL,
    basis_value numeric NOT NULL,
    basis_unit text NOT NULL,
    resolved_substance_iri text REFERENCES substance (iri),
    CONSTRAINT ck_label_extraction__products__ingredients_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_label_extraction__products__ingredients_position__8091bcbd9f CHECK (position >= 0),
    CONSTRAINT uq_label_extraction__products__ingredients_position UNIQUE (owner_iri, position),
    CONSTRAINT ck_label_extraction__products__ingredients_substance_07cd8d704c CHECK (substance_name !~ '[\x01-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_label_extraction__products__ingredients_substance_47b7553e89 CHECK (char_length(substance_name) >= 1),
    CONSTRAINT ck_label_extraction__products__ingredients_substance_c9bb4ecf3d CHECK (char_length(substance_name) <= 500),
    CONSTRAINT ck_label_extraction__products__ingredients_strength__09151b1b37 CHECK (strength_value NOT IN ('NaN', 'Infinity', '-Infinity')),
    CONSTRAINT ck_label_extraction__products__ingredients_strength__6a7785ca0c CHECK (strength_value > 0),
    CONSTRAINT ck_label_extraction__products__ingredients_strength_unit_enum CHECK (strength_unit IN ('1', '[AU]', '[Amb''a''1''U]', '[BAU]', '[arb''U]', '[CCID_50]', '[CFU]', 'Ci', 'd', '[D''ag''U]', 'dL', '[FFU]', '[FNU]', '{GC}', 'g', '[hp_C]', '{kp_C}', '[hp_M]', '[hp_Q]', '[hp_X]', 'h', '[iU]', 'kg', '[Lf]', 'L', 'uCi', 'ug', 'uL', 'umol', 'um', 'mCi', 'meq', 'mg', 'mL', 'mm', 'mmol', 'min', 'mol', 'mo', 'ng', 'nmol', '[NTU]', '[PFU]', '[PNU]', 's', 'cm2', '[TCID_50]', 'U', '[USP''U]', '{VP}', 'wk', 'a')),
    CONSTRAINT ck_label_extraction__products__ingredients_basis_value_finite CHECK (basis_value NOT IN ('NaN', 'Infinity', '-Infinity')),
    CONSTRAINT ck_label_extraction__products__ingredients_basis_val_c0bbedfb57 CHECK (basis_value > 0),
    CONSTRAINT ck_label_extraction__products__ingredients_basis_unit_enum CHECK (basis_unit IN ('1', '[AU]', '[Amb''a''1''U]', '[BAU]', '[arb''U]', '[CCID_50]', '[CFU]', 'Ci', 'd', '[D''ag''U]', 'dL', '[FFU]', '[FNU]', '{GC}', 'g', '[hp_C]', '{kp_C}', '[hp_M]', '[hp_Q]', '[hp_X]', 'h', '[iU]', 'kg', '[Lf]', 'L', 'uCi', 'ug', 'uL', 'umol', 'um', 'mCi', 'meq', 'mg', 'mL', 'mm', 'mmol', 'min', 'mol', 'mo', 'ng', 'nmol', '[NTU]', '[PFU]', '[PNU]', 's', 'cm2', '[TCID_50]', 'U', '[USP''U]', '{VP}', 'wk', 'a')),
    CONSTRAINT ck_label_extraction__products__ingredients_resolved__addec18160 CHECK (resolved_substance_iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$')
);
COMMENT ON TABLE label_extraction__products__ingredients IS 'ExtractedProduct.ingredients: Active ingredients with strengths, in label order. An active ingredient as stated in label narrative: name, strength and the basis of the strength.';
COMMENT ON COLUMN label_extraction__products__ingredients.iri IS 'Canonical identifier (IRI).';
COMMENT ON COLUMN label_extraction__products__ingredients.owner_iri IS 'The owning ExtractedProduct row (label_extraction__products.iri).';
COMMENT ON COLUMN label_extraction__products__ingredients.position IS 'Position in the owner''s list, from 0.';
COMMENT ON COLUMN label_extraction__products__ingredients.substance_name IS 'Substance name exactly as the label states it.';
COMMENT ON COLUMN label_extraction__products__ingredients.strength_value IS 'Numeric strength.';
COMMENT ON COLUMN label_extraction__products__ingredients.strength_unit IS 'Unit of the strength.';
COMMENT ON COLUMN label_extraction__products__ingredients.basis_value IS 'Amount the strength refers to, for example 5 in ''per 5 mL''; 1 for per unit.';
COMMENT ON COLUMN label_extraction__products__ingredients.basis_unit IS 'Unit of the basis: mL in ''per 5 mL'', ''1'' for per tablet or capsule.';
COMMENT ON COLUMN label_extraction__products__ingredients.resolved_substance_iri IS 'The Substance this name resolves to in the reference graph.';

CREATE TABLE label_extraction__products__packages (
    iri text PRIMARY KEY,
    owner_iri text NOT NULL REFERENCES label_extraction__products (iri) ON DELETE CASCADE,
    position integer NOT NULL,
    ndc text NOT NULL,
    resolved_package_iri text REFERENCES packaged_product (iri),
    registry_lists_product boolean,
    CONSTRAINT ck_label_extraction__products__packages_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_label_extraction__products__packages_position_min_inclusive CHECK (position >= 0),
    CONSTRAINT uq_label_extraction__products__packages_position UNIQUE (owner_iri, position),
    CONSTRAINT ck_label_extraction__products__packages_ndc_charset CHECK (ndc !~ '[\x01-\x1f\x7f-\x9f]'),
    CONSTRAINT ck_label_extraction__products__packages_ndc_pattern CHECK (ndc ~ '^([0-9]{4}-[0-9]{4}-[0-9]{2}|[0-9]{5}-[0-9]{3}-[0-9]{2}|[0-9]{5}-[0-9]{4}-[0-9])$'),
    CONSTRAINT ck_label_extraction__products__packages_resolved_pac_fb679e35d4 CHECK (resolved_package_iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_label_extraction__products__packages_rule_listed__fac0d5f639 CHECK (registry_lists_product IS NULL OR registry_lists_product NOT IN (true) OR resolved_package_iri IS NOT NULL)
);
COMMENT ON TABLE label_extraction__products__packages IS 'ExtractedProduct.packages: Package NDCs the label lists for this product. A package NDC mentioned in label narrative (for example in How Supplied).';
COMMENT ON COLUMN label_extraction__products__packages.iri IS 'Canonical identifier (IRI).';
COMMENT ON COLUMN label_extraction__products__packages.owner_iri IS 'The owning ExtractedProduct row (label_extraction__products.iri).';
COMMENT ON COLUMN label_extraction__products__packages.position IS 'Position in the owner''s list, from 0.';
COMMENT ON COLUMN label_extraction__products__packages.ndc IS 'Package NDC in 4-4-2, 5-3-2 or 5-4-1 form.';
COMMENT ON COLUMN label_extraction__products__packages.resolved_package_iri IS 'The PackagedProduct with this NDC in the reference graph.';
COMMENT ON COLUMN label_extraction__products__packages.registry_lists_product IS 'True when the registry knows this label''s products, so its packages must resolve.';
