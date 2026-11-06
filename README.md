# Update a request header in Akamai Test Center

This script finds a test suite by its exact name and sets a request header on
every test case in that suite. Existing matching header names are compared
case-insensitively and all their values are changed. If a test case does not
contain the header, the script adds it with the `ADD` action. Other writable
test-case settings are preserved.

## Setup

Use Python 3.11 or later and install the dependencies:

```shell
python3 -m pip install -r requirements.txt
```

Configure Akamai EdgeGrid credentials in `~/.edgerc`. The default credentials
section is `[default]`; use `--section` to select a different section.

## Run

```shell
python3 update_test_center_header.py "My test suite" "X-Test-Header" "new value"
```

Options:

- `--edgerc PATH`: credentials file (default `~/.edgerc`)
- `--section NAME`: credentials section (default `default`)
- `--account-switch-key KEY`: optional account switch key
- `--timeout SECONDS`: request timeout (default 30)

The script prints a status for each test case (`UNCHANGED`, `QUEUED`,
`UPDATED`, or `FAILED`) and reports Test Center API errors. Header values are
not printed. A suite name must identify exactly one suite.

The script uses Akamai's [Test Center API](https://techdocs.akamai.com/test-ctr/reference):
it lists suites, gets the selected suite with its child objects, and updates
the cases through the bulk test-case update operation.
