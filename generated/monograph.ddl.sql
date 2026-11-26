-- MONOGRAPH drug product model 0.1.0. Generated from core/models.py; do not edit.
-- Run inside an empty schema. Constraint names are stable and used by the integration test.

CREATE TABLE substance (
    iri text PRIMARY KEY,
    unii text NOT NULL,
    name text NOT NULL,
    CONSTRAINT ck_substance_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_substance_unii_pattern CHECK (unii ~ '^[0-9A-Z]{10}$'),
    CONSTRAINT ck_substance_name_min_length CHECK (char_length(name) >= 1),
    CONSTRAINT ck_substance_name_max_length CHECK (char_length(name) <= 500),
    CONSTRAINT uq_substance_key UNIQUE (unii)
);

CREATE TABLE organization (
    iri text PRIMARY KEY,
    slug text NOT NULL,
    name text NOT NULL,
    CONSTRAINT ck_organization_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_organization_slug_pattern CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    CONSTRAINT ck_organization_slug_max_length CHECK (char_length(slug) <= 120),
    CONSTRAINT ck_organization_name_min_length CHECK (char_length(name) >= 1),
    CONSTRAINT ck_organization_name_max_length CHECK (char_length(name) <= 500),
    CONSTRAINT uq_organization_key UNIQUE (slug)
);

CREATE TABLE product (
    iri text PRIMARY KEY,
    rxcui text NOT NULL,
    name text NOT NULL,
    dosage_form text NOT NULL,
    routes text[] NOT NULL DEFAULT '{}',
    CONSTRAINT ck_product_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_product_rxcui_pattern CHECK (rxcui ~ '^[0-9]{1,8}$'),
    CONSTRAINT ck_product_name_min_length CHECK (char_length(name) >= 1),
    CONSTRAINT ck_product_name_max_length CHECK (char_length(name) <= 500),
    CONSTRAINT ck_product_dosage_form_min_length CHECK (char_length(dosage_form) >= 1),
    CONSTRAINT ck_product_dosage_form_max_length CHECK (char_length(dosage_form) <= 100),
    CONSTRAINT ck_product_routes_enum CHECK (routes <@ ARRAY['BUCCAL', 'CUTANEOUS', 'DENTAL', 'EPIDURAL', 'INTRA-ARTICULAR', 'INTRADERMAL', 'INTRAMUSCULAR', 'INTRATHECAL', 'INTRAVENOUS', 'INTRAVESICAL', 'INTRAVITREAL', 'NASAL', 'OPHTHALMIC', 'ORAL', 'OTIC', 'PERCUTANEOUS', 'RECTAL', 'RESPIRATORY (INHALATION)', 'SUBCUTANEOUS', 'SUBLINGUAL', 'TOPICAL', 'TRANSDERMAL', 'VAGINAL']::text[]),
    CONSTRAINT ck_product_routes_min_count CHECK (cardinality(routes) >= 1),
    CONSTRAINT uq_product_key UNIQUE (rxcui)
);

CREATE TABLE product__ingredients (
    iri text PRIMARY KEY,
    owner_iri text NOT NULL REFERENCES product (iri) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    position integer NOT NULL,
    substance_iri text NOT NULL REFERENCES substance (iri) DEFERRABLE INITIALLY IMMEDIATE,
    strength_value numeric NOT NULL,
    strength_unit text NOT NULL,
    CONSTRAINT ck_product__ingredients_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_product__ingredients_position_min_inclusive CHECK (position >= 0),
    CONSTRAINT uq_product__ingredients_position UNIQUE (owner_iri, position),
    CONSTRAINT ck_product__ingredients_substance_iri_pattern CHECK (substance_iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_product__ingredients_strength_value_finite CHECK (strength_value NOT IN ('NaN', 'Infinity', '-Infinity')),
    CONSTRAINT ck_product__ingredients_strength_value_min_exclusive CHECK (strength_value > 0),
    CONSTRAINT ck_product__ingredients_strength_unit_min_length CHECK (char_length(strength_unit) >= 1),
    CONSTRAINT ck_product__ingredients_strength_unit_max_length CHECK (char_length(strength_unit) <= 50)
);

CREATE TABLE packaged_product (
    iri text PRIMARY KEY,
    ndc text NOT NULL,
    product_iri text NOT NULL REFERENCES product (iri) DEFERRABLE INITIALLY IMMEDIATE,
    labeler_iri text NOT NULL REFERENCES organization (iri) DEFERRABLE INITIALLY IMMEDIATE,
    description text,
    CONSTRAINT ck_packaged_product_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_packaged_product_ndc_pattern CHECK (ndc ~ '^([0-9]{4}-[0-9]{4}-[0-9]{2}|[0-9]{5}-[0-9]{3}-[0-9]{2}|[0-9]{5}-[0-9]{4}-[0-9])$'),
    CONSTRAINT ck_packaged_product_product_iri_pattern CHECK (product_iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_packaged_product_labeler_iri_pattern CHECK (labeler_iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_packaged_product_description_min_length CHECK (char_length(description) >= 1),
    CONSTRAINT ck_packaged_product_description_max_length CHECK (char_length(description) <= 2000),
    CONSTRAINT uq_packaged_product_key UNIQUE (ndc)
);

CREATE TABLE label (
    iri text PRIMARY KEY,
    set_id text NOT NULL,
    version bigint NOT NULL,
    effective_date date NOT NULL,
    CONSTRAINT ck_label_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_label_set_id_pattern CHECK (set_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    CONSTRAINT ck_label_version_min_inclusive CHECK (version >= 1),
    CONSTRAINT ck_label_version_max_exclusive CHECK (version < 1000000),
    CONSTRAINT uq_label_key UNIQUE (set_id, version)
);

CREATE TABLE label__products (
    owner_iri text NOT NULL REFERENCES label (iri) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    target_iri text NOT NULL REFERENCES product (iri) DEFERRABLE INITIALLY IMMEDIATE,
    CONSTRAINT ck_label__products_target_iri_pattern CHECK (target_iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT pk_label__products PRIMARY KEY (owner_iri, target_iri)
);

CREATE TABLE label__sections (
    iri text PRIMARY KEY,
    owner_iri text NOT NULL REFERENCES label (iri) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    code text NOT NULL,
    title text NOT NULL,
    text text NOT NULL,
    CONSTRAINT ck_label__sections_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT uq_label__sections_key UNIQUE (owner_iri, code),
    CONSTRAINT ck_label__sections_code_pattern CHECK (code ~ '^[0-9]{1,7}-[0-9]$'),
    CONSTRAINT ck_label__sections_title_min_length CHECK (char_length(title) >= 1),
    CONSTRAINT ck_label__sections_title_max_length CHECK (char_length(title) <= 200),
    CONSTRAINT ck_label__sections_text_min_length CHECK (char_length(text) >= 1)
);

CREATE TABLE adverse_event_report (
    iri text PRIMARY KEY,
    report_id text NOT NULL,
    receipt_date date NOT NULL,
    serious boolean NOT NULL,
    seriousness_criteria text[] NOT NULL DEFAULT '{}',
    patient_age_years numeric,
    CONSTRAINT ck_adverse_event_report_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_adverse_event_report_report_id_pattern CHECK (report_id ~ '^[0-9]{1,12}$'),
    CONSTRAINT ck_adverse_event_report_seriousness_criteria_enum CHECK (seriousness_criteria <@ ARRAY['death', 'life threatening', 'hospitalization', 'disabling', 'congenital anomaly', 'other']::text[]),
    CONSTRAINT ck_adverse_event_report_seriousness_criteria_max_count CHECK (cardinality(seriousness_criteria) <= 6),
    CONSTRAINT ck_adverse_event_report_patient_age_years_finite CHECK (patient_age_years NOT IN ('NaN', 'Infinity', '-Infinity')),
    CONSTRAINT ck_adverse_event_report_patient_age_years_min_inclusive CHECK (patient_age_years >= 0),
    CONSTRAINT ck_adverse_event_report_patient_age_years_max_inclusive CHECK (patient_age_years <= 150),
    CONSTRAINT uq_adverse_event_report_key UNIQUE (report_id),
    CONSTRAINT ck_adverse_event_report_rule_serious_requires_criterion CHECK (serious IS DISTINCT FROM true OR cardinality(seriousness_criteria) >= 1)
);

CREATE TABLE adverse_event_report__drugs (
    iri text PRIMARY KEY,
    owner_iri text NOT NULL REFERENCES adverse_event_report (iri) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    position integer NOT NULL,
    drug_name text NOT NULL,
    product_iri text REFERENCES product (iri) DEFERRABLE INITIALLY IMMEDIATE,
    role text NOT NULL,
    CONSTRAINT ck_adverse_event_report__drugs_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_adverse_event_report__drugs_position_min_inclusive CHECK (position >= 0),
    CONSTRAINT uq_adverse_event_report__drugs_position UNIQUE (owner_iri, position),
    CONSTRAINT ck_adverse_event_report__drugs_drug_name_min_length CHECK (char_length(drug_name) >= 1),
    CONSTRAINT ck_adverse_event_report__drugs_drug_name_max_length CHECK (char_length(drug_name) <= 500),
    CONSTRAINT ck_adverse_event_report__drugs_product_iri_pattern CHECK (product_iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_adverse_event_report__drugs_role_enum CHECK (role IN ('primary suspect', 'secondary suspect', 'suspect', 'concomitant', 'interacting'))
);

CREATE TABLE adverse_event_report__reactions (
    iri text PRIMARY KEY,
    owner_iri text NOT NULL REFERENCES adverse_event_report (iri) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    position integer NOT NULL,
    term text NOT NULL,
    CONSTRAINT ck_adverse_event_report__reactions_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_adverse_event_report__reactions_position_min_inclusive CHECK (position >= 0),
    CONSTRAINT uq_adverse_event_report__reactions_position UNIQUE (owner_iri, position),
    CONSTRAINT ck_adverse_event_report__reactions_term_min_length CHECK (char_length(term) >= 1),
    CONSTRAINT ck_adverse_event_report__reactions_term_max_length CHECK (char_length(term) <= 100)
);

CREATE TABLE recall (
    iri text PRIMARY KEY,
    recall_number text NOT NULL,
    classification text NOT NULL,
    reason text NOT NULL,
    report_date date NOT NULL,
    recalling_firm_iri text NOT NULL REFERENCES organization (iri) DEFERRABLE INITIALLY IMMEDIATE,
    CONSTRAINT ck_recall_iri_pattern CHECK (iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT ck_recall_recall_number_pattern CHECK (recall_number ~ '^D-[0-9]{4}-[0-9]{4}$'),
    CONSTRAINT ck_recall_classification_enum CHECK (classification IN ('I', 'II', 'III')),
    CONSTRAINT ck_recall_reason_min_length CHECK (char_length(reason) >= 1),
    CONSTRAINT ck_recall_recalling_firm_iri_pattern CHECK (recalling_firm_iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT uq_recall_key UNIQUE (recall_number)
);

CREATE TABLE recall__products (
    owner_iri text NOT NULL REFERENCES recall (iri) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    target_iri text NOT NULL REFERENCES product (iri) DEFERRABLE INITIALLY IMMEDIATE,
    CONSTRAINT ck_recall__products_target_iri_pattern CHECK (target_iri ~ '^https?://[A-Za-z0-9._~:/?#@!$&''()*+,;=%\-]+$'),
    CONSTRAINT pk_recall__products PRIMARY KEY (owner_iri, target_iri)
);
