#!/usr/bin/env python3
"""Update a request header across all test cases in an Akamai Test Center suite."""

from __future__ import annotations

import argparse
import configparser
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from akamai.edgegrid import EdgeGridAuth, EdgeRc


API_PATH = "/test-management/v3/functional/test-suites"
HEADER_NAME_PATTERN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
TEST_CASE_FIELDS = ("order", "setVariables")
TEST_REQUEST_FIELDS = (
    "testRequestUrl",
    "requestMethod",
    "requestHeaders",
    "requestBody",
    "encodeRequestBody",
    "tags",
)
CLIENT_PROFILE_FIELDS = ("client", "ipVersion", "geoLocation")
VARIABLE_FIELDS = ("variableName", "variableId", "variableValue")


class ApiError(RuntimeError):
    """An HTTP or response-format error returned by Test Center."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Set a request header value across a Test Center test suite."
    )
    parser.add_argument("suite_name", help="Exact name of the test suite")
    parser.add_argument("header_name", help="Request header name to set")
    parser.add_argument("new_value", help="New request header value")
    parser.add_argument(
        "--edgerc",
        type=Path,
        default=Path.home() / ".edgerc",
        help="Path to the EdgeGrid credentials file (default: ~/.edgerc)",
    )
    parser.add_argument(
        "--section",
        default="default",
        help="Credentials section in the EdgeGrid file (default: default)",
    )
    parser.add_argument(
        "--account-switch-key",
        help="Optional Akamai account switch key",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30)",
    )
    args = parser.parse_args()

    if not args.suite_name.strip():
        parser.error("suite_name must not be empty")
    if not HEADER_NAME_PATTERN.fullmatch(args.header_name):
        parser.error("header_name must be a valid HTTP header name")
    if not args.new_value:
        parser.error("new_value must not be empty")
    if "\r" in args.new_value or "\n" in args.new_value:
        parser.error("new_value must not contain carriage returns or newlines")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    return args


def api_url(host: str) -> str:
    host = host.strip()
    if not host.startswith(("https://", "http://")):
        host = f"https://{host}"
    parsed = urlsplit(host)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
        raise ValueError("the EdgeGrid host must be an HTTPS host without a path")
    return f"https://{parsed.netloc}"


def response_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError as error:
        raise ApiError(
            f"Test Center returned invalid JSON (HTTP {response.status_code})."
        ) from error


def request_json(
    session: requests.Session,
    base_url: str,
    method: str,
    path: str,
    timeout: float,
    *,
    params: dict[str, str] | None = None,
    body: Any = None,
) -> Any:
    try:
        response = session.request(
            method,
            f"{base_url}{path}",
            params=params,
            json=body,
            timeout=timeout,
        )
    except requests.exceptions.RequestException as error:
        raise ApiError(
            f"{method} {path} did not receive an HTTP response: {error}"
        ) from error
    if not response.ok:
        detail = response.text.strip()
        if len(detail) > 1000:
            detail = f"{detail[:1000]}..."
        raise ApiError(
            f"{method} {path} failed with HTTP {response.status_code}"
            f"{': ' + detail if detail else '.'}"
        )
    return response_json(response)


def get_test_case_update(
    test_case: dict[str, Any], header_name: str, new_value: str
) -> dict[str, Any] | None:
    """Build a writable API payload if the case's header value needs changing."""
    test_request = test_case.get("testRequest")
    condition = test_case.get("condition")
    client_profile = test_case.get("clientProfile")
    if not isinstance(test_request, dict):
        raise ApiError("A test case is missing its testRequest object.")
    if not isinstance(condition, dict) or "conditionExpression" not in condition:
        raise ApiError("A test case is missing its condition expression.")
    if not isinstance(client_profile, dict):
        raise ApiError("A test case is missing its clientProfile object.")
    if "testCaseId" not in test_case:
        raise ApiError("A test case is missing its testCaseId.")

    headers = test_request.get("requestHeaders") or []
    if not isinstance(headers, list):
        raise ApiError("A test case has an invalid requestHeaders value.")

    matching_header_found = False
    header_changed = False
    writable_headers: list[dict[str, Any]] = []
    for header in headers:
        if not isinstance(header, dict):
            raise ApiError("A test case contains an invalid request header.")
        if not all(key in header for key in ("headerName", "headerValue", "headerAction")):
            raise ApiError("A request header is missing a required field.")
        writable_header = {
            key: header[key]
            for key in ("headerName", "headerValue", "headerAction")
        }
        if str(header["headerName"]).casefold() == header_name.casefold():
            matching_header_found = True
            if header["headerValue"] != new_value:
                writable_header["headerValue"] = new_value
                header_changed = True
        writable_headers.append(writable_header)

    if not matching_header_found:
        writable_headers.append(
            {
                "headerName": header_name,
                "headerValue": new_value,
                "headerAction": "ADD",
            }
        )
        header_changed = True

    if not header_changed:
        return None

    update: dict[str, Any] = {
        "testCaseId": test_case["testCaseId"],
        "condition": {"conditionExpression": condition["conditionExpression"]},
        "clientProfile": {
            key: client_profile[key]
            for key in CLIENT_PROFILE_FIELDS
            if key in client_profile
        },
        "testRequest": {
            key: test_request[key]
            for key in TEST_REQUEST_FIELDS
            if key in test_request and key != "requestHeaders"
        },
    }
    update["testRequest"]["requestHeaders"] = writable_headers

    for key in TEST_CASE_FIELDS:
        if key in test_case:
            if key == "setVariables":
                if not isinstance(test_case[key], list):
                    raise ApiError("A test case has an invalid setVariables value.")
                update[key] = [
                    {
                        field: variable[field]
                        for field in VARIABLE_FIELDS
                        if field in variable
                    }
                    for variable in test_case[key]
                ]
            else:
                update[key] = test_case[key]
    return update


def case_label(test_case: dict[str, Any]) -> str:
    case_id = test_case.get("testCaseId", "unknown")
    order = test_case.get("order", "unknown")
    return f"testCaseId={case_id}, order={order}"


def failure_case_ids(failure: dict[str, Any]) -> set[int]:
    if failure.get("requestField") != "testCaseId":
        return set()
    request_values = failure.get("requestValues")
    if not isinstance(request_values, list):
        return set()
    return {value for value in request_values if type(value) is int}


def run(args: argparse.Namespace) -> int:
    edgerc = EdgeRc(str(args.edgerc.expanduser()))
    host = edgerc.get(args.section, "host")
    base_url = api_url(host)

    session = requests.Session()
    session.auth = EdgeGridAuth.from_edgerc(edgerc, args.section)
    params = (
        {"accountSwitchKey": args.account_switch_key}
        if args.account_switch_key
        else None
    )

    suite_list = request_json(
        session, base_url, "GET", API_PATH, args.timeout, params=params
    )
    if not isinstance(suite_list, dict) or not isinstance(
        suite_list.get("testSuites"), list
    ):
        raise ApiError("The test suite list response did not contain a testSuites array.")

    matches = [
        suite
        for suite in suite_list["testSuites"]
        if isinstance(suite, dict) and suite.get("testSuiteName") == args.suite_name
    ]
    if not matches:
        raise ApiError(f"No test suite named {args.suite_name!r} was found.")
    if len(matches) != 1:
        raise ApiError(
            f"More than one test suite is named {args.suite_name!r}; "
            "use a unique suite name."
        )

    suite_id = matches[0].get("testSuiteId")
    if not isinstance(suite_id, int):
        raise ApiError("The matched test suite did not include a valid testSuiteId.")

    suite = request_json(
        session,
        base_url,
        "GET",
        f"{API_PATH}/{suite_id}/with-child-objects",
        args.timeout,
        params={"resolveVariables": "false", **(params or {})},
    )
    if not isinstance(suite, dict) or not isinstance(suite.get("testCases"), list):
        raise ApiError("The test suite response did not contain a testCases array.")

    if not suite["testCases"]:
        print(f"No test cases found in suite {args.suite_name!r}.")
        return 0

    updates: list[dict[str, Any]] = []
    unchanged_count = 0
    print(
        f"Checking {len(suite['testCases'])} test case(s) for "
        f"header {args.header_name!r}...",
        file=sys.stderr,
    )
    for case in suite["testCases"]:
        if not isinstance(case, dict):
            raise ApiError("A test case entry is not a JSON object.")
        label = case_label(case)
        try:
            update = get_test_case_update(case, args.header_name, args.new_value)
        except ApiError as error:
            raise ApiError(f"{label}: {error}") from error

        if update is None:
            unchanged_count += 1
            print(
                f"[UNCHANGED] {label}: requested header value is already set.",
                file=sys.stderr,
            )
        else:
            updates.append(update)
            print(
                f"[QUEUED] {label}: header change will be submitted.",
                file=sys.stderr,
            )

    if not updates:
        print(
            f"No changes needed: {unchanged_count} test case(s) in "
            f"{args.suite_name!r} already have the requested header value."
        )
        return 0

    result = request_json(
        session,
        base_url,
        "PUT",
        f"{API_PATH}/{suite_id}/test-cases",
        args.timeout,
        params=params,
        body=updates,
    )
    if not isinstance(result, dict):
        raise ApiError("The update response was not a JSON object.")

    successes = result.get("successes", [])
    failures = result.get("failures", [])
    if not isinstance(successes, list) or not isinstance(failures, list):
        raise ApiError(
            "The update response did not contain valid successes/failures lists."
        )

    success_ids = {
        success.get("testCaseId")
        for success in successes
        if isinstance(success, dict)
        and type(success.get("testCaseId")) is int
    }
    failures_by_id: dict[int, list[dict[str, Any]]] = {}
    unassigned_failures: list[dict[str, Any]] = []
    for failure in failures:
        if not isinstance(failure, dict):
            raise ApiError("The update response contains an invalid failure entry.")
        case_ids = failure_case_ids(failure)
        if not case_ids:
            unassigned_failures.append(failure)
            continue
        for case_id in case_ids:
            failures_by_id.setdefault(case_id, []).append(failure)

    for update in updates:
        case_id = update["testCaseId"]
        label = f"testCaseId={case_id}, order={update.get('order', 'unknown')}"
        if case_id in success_ids:
            print(
                f"[UPDATED] {label}: Test Center confirmed the update.",
                file=sys.stderr,
            )
        elif case_id in failures_by_id:
            details = json.dumps(failures_by_id[case_id], ensure_ascii=False)
            print(f"[FAILED] {label}: {details}", file=sys.stderr)
        else:
            print(
                f"[NO RESULT] {label}: not listed in the API successes or failures.",
                file=sys.stderr,
            )

    for failure in unassigned_failures:
        print(
            f"[API FAILURE] Could not associate this failure with one test case: "
            f"{json.dumps(failure, ensure_ascii=False)}",
            file=sys.stderr,
        )

    print(
        f"Updated {len(successes)} test case(s); "
        f"{len(failures)} failure(s) in suite {args.suite_name!r} "
        f"(ID {suite_id})."
    )
    if failures or len(success_ids) != len(updates):
        return 1
    return 0


def main() -> int:
    args = parse_args()
    try:
        return run(args)
    except (
        ApiError,
        configparser.Error,
        OSError,
        KeyError,
        ValueError,
        requests.exceptions.RequestException,
    ) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
