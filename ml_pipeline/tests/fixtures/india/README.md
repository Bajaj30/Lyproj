# Test fixture — NOT the medicines dataset

15 brands and 6 NLEM molecules, so the pipeline and the API can be exercised on a
machine without the real CSVs (`ml_pipeline/data/india/*.csv` is gitignored).

    PRESCRIPTAI_DATA_DIR=ml_pipeline/tests/fixtures/india

Brand names and compositions are the ones quoted in the implementation journal;
strengths are the common marketed ones, entered by hand for tests. Manufacturer is
`TEST FIXTURE` on every row so a fixture row can never be mistaken for real data.
Never point a deployment at this directory.
