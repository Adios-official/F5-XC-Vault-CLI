#!/usr/bin/env python3
import argparse
import subprocess
import os
import base64
import tempfile
import json
import re
import sys

# ==========================================
# VALIDATION ENGINE
# ==========================================
def validate_local_files(filepaths):
    """Checks if all required files exist before executing any API logic."""
    for name, path in filepaths.items():
        if not os.path.isfile(path):
            print(f"[ERROR] Required file missing: The {name} was not found at '{path}'", file=sys.stderr)
            sys.exit(1)

def verify_modulus(cert_path, key_path):
    """
    Uses OpenSSL to verify that the Public Cert and Private Key mathematically match.
    (Requires OpenSSL to be installed on the system/runner).
    """
    try:
        # Get cert modulus
        cert_mod_cmd = ["openssl", "x509", "-noout", "-modulus", "-in", cert_path]
        cert_res = subprocess.run(cert_mod_cmd, capture_output=True, text=True, check=True)
        cert_mod = cert_res.stdout.strip()

        # Get key modulus
        key_mod_cmd = ["openssl", "rsa", "-noout", "-modulus", "-in", key_path]
        key_res = subprocess.run(key_mod_cmd, capture_output=True, text=True, check=True)
        key_mod = key_res.stdout.strip()

        if cert_mod == key_mod:
            return True, "[SUCCESS] Cryptographic modulus match confirmed. Key pair is valid."
        else:
            return False, "[ERROR] Modulus mismatch! The private key does NOT belong to this certificate."
            
    except subprocess.CalledProcessError as e:
        # This usually triggers if the key is password protected or the file is malformed
        return False, f"[ERROR] OpenSSL failed to read the keys. Are they standard PEM format? Details: {e.stderr.strip()}"
    except FileNotFoundError:
        print("[WARNING] OpenSSL is not installed on this system. Skipping modulus check.", file=sys.stderr)
        return True, "" # Proceed anyway if OpenSSL isn't available

# ==========================================
# BACKEND CORE: F5 BLINDFOLDER
# ==========================================
class F5Blindfolder:
    def __init__(self, tenant, p12_path, p12_pass):
        self.tenant = tenant
        self.p12_path = p12_path
        self.p12_pass = p12_pass
        self.url = f"https://{tenant}.console.ves.volterra.io/api"
        fd, self.config_path = tempfile.mkstemp(suffix=".yaml", text=True)
        os.close(fd)

    def _create_config(self):
        config_content = f"server-urls: {self.url}\np12-bundle: {self.p12_path}\n"
        with open(self.config_path, "w") as f:
            f.write(config_content)

    def _run_vesctl(self, args):
        self._create_config()
        env = os.environ.copy()
        env["VES_P12_PASSWORD"] = self.p12_pass
        cmd = ["vesctl", "--config", self.config_path, "--timeout", "60"] + args
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, env=env)
            if res.returncode != 0:
                return {"success": False, "error": res.stderr.strip()}
            return {"success": True, "output": res.stdout.strip()}
        finally:
            if os.path.exists(self.config_path):
                os.remove(self.config_path)

    def check_certificate_exists(self, name, namespace):
        res = self._run_vesctl(["configuration", "get", "certificate", name, "--namespace", namespace])
        return res["success"]

    def blindfold(self, raw_secret, pol_name, pol_ns):
        pk_res = self._run_vesctl(["request", "secrets", "get-public-key"])
        if not pk_res["success"]: 
            raise RuntimeError(f"Failed to get Public Key: {pk_res['error']}")
        
        pol_res = self._run_vesctl(["request", "secrets", "get-policy-document", "--namespace", pol_ns, "--name", pol_name])
        if not pol_res["success"]:
            raise RuntimeError(f"Failed to get Policy: {pol_res['error']}")
        
        files = {'pub': 't_pub.key', 'pol': 't_pol.doc', 'sec': 't_sec.txt'}
        with open(files['pub'], "w") as f: f.write(pk_res["output"])
        with open(files['pol'], "w") as f: f.write(pol_res["output"])
        with open(files['sec'], "w") as f: f.write(raw_secret)
        
        try:
            enc_res = self._run_vesctl(["request", "secrets", "encrypt", "--policy-document", files['pol'], "--public-key", files['pub'], files['sec']])
            if not enc_res["success"]:
                raise RuntimeError(f"Encryption failed: {enc_res['error']}")
            
            clean_blob = enc_res["output"].replace("Encrypted Secret (Base64 encoded):", "")
            clean_blob = re.sub(r'\s+', '', clean_blob)
            return f"string:///{clean_blob}"
        finally:
            for f_path in files.values():
                if os.path.exists(f_path): os.remove(f_path)

    def upload_to_console(self, payload_dict, action="create"):
        fd, json_path = tempfile.mkstemp(suffix=".json", text=True)
        os.close(fd)
        try:
            with open(json_path, "w") as f:
                json.dump(payload_dict, f)
            res = self._run_vesctl(["configuration", action, "certificate", "-i", json_path])
            return res
        finally:
            if os.path.exists(json_path):
                os.remove(json_path)

def process_certificate(cert_string):
    b64_cert = base64.b64encode(cert_string.encode("utf-8")).decode("utf-8")
    return f"string:///{b64_cert}"

# ==========================================
# CLI APPLICATION LOGIC
# ==========================================
def main():
    parser = argparse.ArgumentParser(description="F5XC Vault: CLI for Cryptographic Asset Blindfolding and Deployment")
    
    # Infrastructure Args
    parser.add_argument("--tenant", required=True, help="Tenant Prefix (e.g., sdc-support)")
    parser.add_argument("--p12-path", required=True, help="Path to the .p12 API Credential file")
    parser.add_argument("--p12-password", help="Credential Password (or use VES_P12_PASSWORD env var)")
    
    parser.add_argument("--pol-name", default="ves-io-allow-volterra", help="Blindfold Policy Name (default: ves-io-allow-volterra)")
    parser.add_argument("--pol-ns", default="shared", help="Policy Namespace (default: shared)")
    
    # Metadata Args
    parser.add_argument("--cert-name", required=True, help="New Certificate Object Name")
    parser.add_argument("--cert-ns", required=True, help="Destination Namespace for the Certificate")
    
    # Asset Args
    parser.add_argument("--cert-file", required=True, help="Path to the Public Cert (.pem or .crt)")
    parser.add_argument("--key-file", required=True, help="Path to the Private Key (.key or .pem)")
    
    # Modifiers
    parser.add_argument("--dry-run", action="store_true", help="Generate and print the JSON payload to stdout without uploading")
    parser.add_argument("--force", "-y", action="store_true", help="Bypass interactive prompts and auto-overwrite if the certificate already exists (use in CI/CD)")
    
    args = parser.parse_args()

    # 1. Determine Password securely
    p12_password = args.p12_password or os.environ.get("VES_P12_PASSWORD")
    if not p12_password:
        print("[ERROR] .p12 password must be provided via --p12-password or VES_P12_PASSWORD env var.", file=sys.stderr)
        sys.exit(1)

    # 2. Local File Validations
    print("[INFO] Validating local files...", file=sys.stderr)
    validate_local_files({
        "API Credential": args.p12_path,
        "Public Certificate": args.cert_file,
        "Private Key": args.key_file
    })
    
    # 3. Cryptographic Modulus Validation
    is_valid, mod_msg = verify_modulus(args.cert_file, args.key_file)
    if not is_valid:
        print(mod_msg, file=sys.stderr)
        sys.exit(1)
    if mod_msg:
        print(mod_msg, file=sys.stderr)

    # Read the validated files into memory
    with open(args.cert_file, 'r') as f: cert_input = f.read().strip()
    with open(args.key_file, 'r') as f: key_input = f.read().strip()

    try:
        print(f"[INFO] Connecting to tenant '{args.tenant}'...", file=sys.stderr)
        bf = F5Blindfolder(args.tenant, args.p12_path, p12_password)
        
        # --- STATE CHECK & INTERACTIVE PROMPT ---
        print(f"[INFO] Checking if certificate '{args.cert_name}' exists in namespace '{args.cert_ns}'...", file=sys.stderr)
        cert_exists = bf.check_certificate_exists(args.cert_name, args.cert_ns)
        
        api_action = "create"
        
        if cert_exists:
            print(f"\n[WARNING] Certificate '{args.cert_name}' already exists in namespace '{args.cert_ns}'.", file=sys.stderr)
            if not args.force and not args.dry_run:
                choice = input("Do you want to OVERWRITE this certificate? [y/N]: ").strip().lower()
                if choice not in ['y', 'yes']:
                    print("[INFO] Aborting operation at user request. No changes were made.", file=sys.stderr)
                    sys.exit(0)
            api_action = "replace"
        
        print(f"[INFO] Initializing blindfold encryption...", file=sys.stderr)
        f_key = bf.blindfold(key_input, args.pol_name, args.pol_ns)
        
        final_payload = {
            "metadata": {"name": args.cert_name, "namespace": args.cert_ns},
            "spec": {
                "certificate_url": process_certificate(cert_input),
                "private_key": {"blindfold_secret_info": {"location": f_key}}
            }
        }
        
        if args.dry_run:
            print("[INFO] Dry run enabled. Outputting payload:", file=sys.stderr)
            print(json.dumps(final_payload, indent=4))
            sys.exit(0)
            
        print(f"[INFO] Pushing payload (Action: {api_action.upper()})...", file=sys.stderr)
        result = bf.upload_to_console(final_payload, action=api_action)
        
        if result["success"]:
            print(f"[SUCCESS] Deployment Successful ({api_action}).", file=sys.stderr)
            print(result["output"])
        else:
            print(f"[ERROR] Upload Failed: {result['error']}", file=sys.stderr)
            sys.exit(1)

    except Exception as e:
        print(f"[ERROR] Process failed: {str(e)}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
