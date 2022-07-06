#!/usr/bin/env python3
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""ASF Infrastructure Download Integrity Checker"""
import os
import gnupg
import yaml
import asfpy.messaging
import hashlib
import requests
import time
import sys
import string

CHUNK_SIZE = 4096
CFG = yaml.safe_load(open("./checker.yaml"))
assert CFG.get("gpg_homedir"), "Please specify a homedir for the GPG keychain!"

WHIMSY_MAIL_MAP = "https://whimsy.apache.org/public/committee-info.json"
MAIL_MAP = requests.get(WHIMSY_MAIL_MAP).json()["committees"]
EMAIL_TEMPLATE = open("email-template.txt", "r").read()
INTERVAL = 1800  # Sleep for 30 min if --forever is set, then repeat


def alert_project(project: str, errors: list):
    """Sends a notification to the project and infra aboot errors that were found"""
    if errors:
        project_list = f"private@{project}.apache.org"  # Standard naming
        if project in MAIL_MAP:
            project_list = f"private@{MAIL_MAP[project]['mail_list']}.apache.org"  # Special case for certain committees
        print(f"Dispatching notification to {project_list}!")
        recipients = [project_list]
        extra_recips = CFG.get("extra_recipients")
        if isinstance(extra_recips, list):
            recipients.extend(extra_recips)
        errormsg = ""
        for filepath, errorlines in errors.items():
            errormsg += f"Errors were found while verifying {filepath}:\n"
            for errorline in errorlines:
                errormsg += f" - {errorline}\n"
            errormsg += "\n"
        if "--debug" not in sys.argv:  # Don't send emails if --debug is specified
            asfpy.messaging.mail(
                sender="ASF Infrastructure <root@apache.org>",
                subject=f"Verification of download artefacts on dist.apache.org FAILED for {project}!",
                recipients=recipients,
                message=EMAIL_TEMPLATE.format(**locals())
            )
        else:
            print("Debug flag active, not sending email. But it would have looked like this:\n")
            print(EMAIL_TEMPLATE.format(**locals()))


def load_keys(project: str) -> gnupg.GPG:
    """Loads all keys found in KEYS files for a project and returns the GPG toolchain object holding said keys"""
    project_dir = os.path.join(CFG["dist_dir"], project)
    project_gpg_dir = os.path.join(CFG["gpg_homedir"], project)
    assert project and os.path.isdir(project_dir), f"Project not specified or no project dist directory found for {project}!"
    if not os.path.isdir(project_gpg_dir):
        os.mkdir(project_gpg_dir)
    keychain = gnupg.GPG(gnupghome=project_gpg_dir)
    for root, dirs, files in os.walk(project_dir):
        for filename in files:
            filepath = os.path.join(root, filename)
            if filename in ["KEYS", "KEYS.txt"]:
                if "--quiet" not in sys.argv:
                    print(f"Loading {filepath} into toolchain")
                keychain.import_keys(open(filepath, "rb").read())
    return keychain


def digest(filepath: str, method: str):
    """Calculates and returns the checksum of a file given a file path and a digest method (sha256, sha512 etc)"""
    digester = hashlib.new(method)
    with open(filepath, "rb") as file:
        for chunk in iter(lambda: file.read(CHUNK_SIZE), b''):
            digester.update(chunk)
    return digester.hexdigest()


def verify_checksum(filepath: str, method: str):
    """Verifies a filepath against its checksum file, given a checksum method. Returns a list of errors if any found"""
    filename = os.path.basename(filepath)
    checksum_filepath = filepath + "." + method  # foo.sha256
    checksum_filename = os.path.basename(checksum_filepath)
    checksum_options = open(checksum_filepath, "r").read().strip().split(" ")
    checksum_on_disk = "".join(x.strip() for x in checksum_options if all(c in string.hexdigits for c in x.strip())).lower()
    checksum_calculated = digest(filepath, method)
    errors = []
    if checksum_on_disk != checksum_calculated:
        errors.append(f"Checksum does not match checksum file {checksum_filename}!")
        errors.append(f"Calculated {method} checksum of {filename} was: {checksum_calculated}")
        errors.append(f"Checksum file {checksum_filename} said it should have been: {checksum_on_disk}")
    return errors


def push_error(edict: dict, filepath: str, errmsg: str):
    """Push an error message to the error dict, creating an entry if none exists, otherwise appending to it"""
    if filepath not in edict:
        edict[filepath] = list()
    if isinstance(errmsg, list):
        edict[filepath].extend(errmsg)
    else:
        edict[filepath].append(errmsg)


def verify_files(project: str, keychain: gnupg.GPG) -> dict:
    """Verifies all download artefacts in a directory using the supplied keychain. Returns a dict of filenames and
    their corresponding error messages if checksum or signature errors were found."""
    errors = dict()
    path = os.path.join(CFG["dist_dir"], project)
    known_exts = CFG.get("known_extensions")
    known_fingerprints = {key["fingerprint"]: key for key in keychain.list_keys()}
    strong_checksum_deadline = CFG.get("strong_checksum_deadline", 0)  # If applicable, only require sha1/md5 for older files
    for root, dirs, files in os.walk(path):
        for filename in sorted(files):
            extension = filename.split(".")[-1] if "." in filename else ""
            if extension in known_exts:
                filepath = os.path.join(root, filename)
                if "--quiet" not in sys.argv:
                    print(f"Verifying {filepath}")
                valid_checksums_found = 0
                # Verify strong checksums
                for method in CFG.get("strong_checksums"):
                    chkfile = filepath + "." + method
                    if os.path.exists(chkfile):
                        file_errors = verify_checksum(filepath, method)
                        if file_errors:
                            push_error(errors, filepath, file_errors)
                        else:
                            valid_checksums_found += 1

                # If no valid strong checksums, but the files are older, check against sha1 and md5?
                if valid_checksums_found == 0 and os.stat(filepath).st_mtime <= strong_checksum_deadline:

                    for method in CFG.get("weak_checksums"):
                        chkfile = filepath + "." + method
                    if os.path.exists(chkfile):
                        file_errors = verify_checksum(filepath, method)
                        if file_errors:
                            push_error(errors, filepath, file_errors)
                        else:
                            valid_checksums_found += 1
                    # Ensure we had at least one valid checksum file of any kind.
                    if valid_checksums_found == 0:
                        push_error(errors, filepath, f"No valid checksum files (.md5, .sha1, .sha256, .sha512) found for {filename}")

                # Ensure we had at least one (valid) sha256 or sha512 file if strong checksums are enforced.
                elif valid_checksums_found == 0:
                    push_error(errors, filepath, f"No valid checksum files (.sha256, .sha512) found for {filename}")

                # Verify detached signatures
                asc_filepath = filepath + ".asc"
                if os.path.exists(asc_filepath):
                    verified = keychain.verify_file(open(asc_filepath, "rb"), data_filename=filepath)
                    if not verified.valid:
                        if verified.fingerprint not in known_fingerprints:
                            push_error(errors, filepath, f"The signature file {filename} was signed with a fingerprint not found in the project's KEYS file: {verified.fingerprint}")
                        else:
                            fp = known_fingerprints[verified.fingerprint]
                            fp_expires = int(fp["expires"])
                            # Check if key expired before signing
                            if fp_expires < int(verified.sig_timestamp):
                                fp_owner = fp["uids"][0]
                                push_error(errors, filepath, f"Detached signature file {filename}.asc was signed by {fp_owner} ({verified.fingerprint}) but the key has expired!")
                            # Otherwise, check for anything that isn't "signature valid"
                            elif verified.status != "signature valid":
                                push_error(errors, filepath, f"Detached signature file {filename}.asc could not be used to verify {filename}: {verified.status}")
    return errors


def main():
    start_time = time.time()
    gpg_home = CFG["gpg_homedir"]
    if not os.path.isdir(gpg_home):
        print(f"Setting up GPG homedir in {gpg_home}")
        os.mkdir(gpg_home)
    projects = [x for x in os.listdir(CFG["dist_dir"]) if os.path.isdir(os.path.join(CFG["dist_dir"], x))]

    # Quick hack for only scanning certain dirs by adding the project name(s) to the command line
    x_projects = []
    for arg in sys.argv:
        if arg in projects:
            x_projects.append(arg)
    if x_projects:
        projects = x_projects

    while True:
        for project in projects:
            print(f"Scanning {project}")
            start_time_project = time.time()
            keychain = load_keys(project)
            errors = verify_files(project, keychain)
            if errors:
                print(f"Errors were found while verifying {project}!")
                alert_project(project, errors)
            time_taken = int(time.time() - start_time_project)
            print(f"{project} scanned in {time_taken} seconds.")
        total_time_taken = int(time.time() - start_time)
        print(f"Done scanning {len(projects)} projects in {total_time_taken} seconds.")
        if "--forever" in sys.argv:
            print(f"Sleeping for {INTERVAL} seconds.")
            time.sleep(INTERVAL)
        else:
            break


if __name__ == "__main__":
    main()

