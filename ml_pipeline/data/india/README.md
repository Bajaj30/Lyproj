# Put the medicine data here

Two files, not in git (18 MB+; see the root README §9):

    indian_medicines_master.csv      239,640 brands: Brand Name, Composition, Strength, Form, Manufacturer
    nlem2022_formulations.csv        NLEM 2022: molecule, aliases

Without them the brand matching (S2/S3) cannot run: uploads fail with "The
medicine database is not installed on this server", and `/api/health` reports
`"medicine_data": "missing"`. Get them from a teammate.

Docker mounts this folder read-only into the container, so copying the files
here and restarting is enough; no rebuild.

To try the product before you have them, point the loader at the 15-brand test
fixture (never for real use):

    PRESCRIPTAI_DATA_DIR=ml_pipeline/tests/fixtures/india
