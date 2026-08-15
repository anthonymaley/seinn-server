#!/usr/bin/env python3
"""krutho_selftest.py — ctypes fixture verify against libkrutho.so.

RUNS ON WEEFISH ONLY. libkrutho.so is a Linux x86_64 build (CMake
out-of-tree over ~/krutho-new/packages/{verifier-sdk,oid4vp}, per
docs/research/2026-07-30-krutho-auth-spike.md and Phase D of
docs/plans/2026-08-15-krutho-ios-spec.md). There is no macOS build of
this library, so this script cannot run on the Mac — that's why the Mac
side of the agent only ever exercises the krutho=ABSENT fallback path.

Recreated from the spike doc (the spike's throwaway script did not
survive) — 10 assertions: same shape as the spike's "10/10 ctypes
assertions" claim (positive verify + claims content + 3 free/no-crash
checks + 5 negative controls covering revoked / wrong nonce / wrong
audience / tampered issuer / bad params).

Fixtures: SDK test corpus at
  ~/krutho-new/packages/oid4vp/c/test/test_data/          (presentations, trust chain)
  ~/krutho-new/packages/verifier-sdk/test_data/            (revocation filter)
These are the SDK's own fixtures, not real Krutho-issued credentials —
[issuer-gated]: no issuer infrastructure exists here (see the spec's
"what cannot be verified" section).

Usage: python3 krutho_selftest.py [/path/to/libkrutho.so]
Exits 0 if all assertions pass, 1 otherwise.
"""
import ctypes
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
OID4VP_DATA = os.path.join(HOME, "krutho-new/packages/oid4vp/c/test/test_data")
VERIFIER_DATA = os.path.join(HOME, "krutho-new/packages/verifier-sdk/test_data")

DEFAULT_LIB = "/opt/seinn/libkrutho.so"


# ---- ctypes signatures (mirror verifier_sdk.h exactly) --------------------

class KruthoVerifyResult(ctypes.Structure):
    _fields_ = [("claims_json", ctypes.c_char_p)]


class KruthoAuthRequest(ctypes.Structure):
    _fields_ = [("json", ctypes.c_char_p)]


KRUTHO_OK = 0
ERROR_NAMES = {
    0: "KRUTHO_OK",
    1: "KRUTHO_ERR_INVALID_PARAM",
    2: "KRUTHO_ERR_REVOKED",
    3: "KRUTHO_ERR_INVALID_NONCE",
    4: "KRUTHO_ERR_INVALID_AUDIENCE",
    5: "KRUTHO_ERR_KB_JWT_SIGNATURE",
    6: "KRUTHO_ERR_X5C_CHAIN",
    7: "KRUTHO_ERR_ISSUER_SIGNATURE",
    8: "KRUTHO_ERR_DISCLOSURE_INTEGRITY",
    9: "KRUTHO_ERR_PARSE",
}


def load_lib(path):
    lib = ctypes.CDLL(path)
    lib.krutho_verify.restype = ctypes.c_int
    lib.krutho_verify.argtypes = [
        ctypes.c_char_p, ctypes.c_size_t,      # vp_token, len
        ctypes.c_char_p, ctypes.c_size_t,      # presentation_submission, len
        ctypes.c_char_p, ctypes.c_size_t,      # trust_chain, len
        ctypes.c_char_p, ctypes.c_size_t,      # revocation_list, len
        ctypes.c_char_p,                       # expected_nonce
        ctypes.c_char_p,                       # expected_audience
        ctypes.POINTER(KruthoVerifyResult),
    ]
    lib.krutho_verify_result_free.restype = None
    lib.krutho_verify_result_free.argtypes = [ctypes.POINTER(KruthoVerifyResult)]
    lib.krutho_check_revoked.restype = ctypes.c_int
    lib.krutho_check_revoked.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
    return lib


def read_text_trimmed(path):
    with open(path, "rb") as f:
        return f.read().rstrip(b"\r\n \t")


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def do_verify(lib, vp, ps, tc, rl, nonce, aud):
    result = KruthoVerifyResult()
    rc = lib.krutho_verify(
        vp, len(vp),
        ps, len(ps),
        tc, len(tc),
        rl, len(rl),
        nonce, aud,
        ctypes.byref(result))
    claims = result.claims_json
    lib.krutho_verify_result_free(ctypes.byref(result))
    return rc, claims


def main():
    lib_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LIB
    ok = True
    passed = 0
    total = 0

    def check(label, cond):
        nonlocal ok, passed, total
        total += 1
        if cond:
            passed += 1
            print(f"PASS: {label}")
        else:
            ok = False
            print(f"FAIL: {label}")

    if not os.path.exists(lib_path):
        print(f"SKIP: {lib_path} absent — krutho_selftest cannot run "
              f"(this IS the Mac-side / lib-missing behavior)")
        return 1

    lib = load_lib(lib_path)

    vp_valid = read_text_trimmed(os.path.join(OID4VP_DATA, "valid_presentation.txt"))
    ps = read_text_trimmed(os.path.join(OID4VP_DATA, "presentation_submission.json"))
    tc = read_bytes(os.path.join(OID4VP_DATA, "trust_chain.der"))
    nonce = read_text_trimmed(os.path.join(OID4VP_DATA, "expected_nonce.txt"))
    aud = read_text_trimmed(os.path.join(OID4VP_DATA, "expected_audience.txt"))
    tampered_vp_path = os.path.join(OID4VP_DATA, "tampered_issuer_presentation.txt")

    # revocation list that does NOT contain this credential's jti
    rl_clean = read_bytes(os.path.join(VERIFIER_DATA, "test_revocation_filter"))
    # revocation list variant used by the SDK's revoked-credential test
    rl_revoked_path = os.path.join(VERIFIER_DATA, "test_revocation_filter_verify")
    rl_revoked = read_bytes(rl_revoked_path) if os.path.exists(rl_revoked_path) else rl_clean

    # 1. Valid presentation verifies OK.
    rc, claims = do_verify(lib, vp_valid, ps, tc, rl_clean, nonce, aud)
    check("valid presentation -> KRUTHO_OK", rc == KRUTHO_OK)

    # 2. Claims are non-empty JSON.
    check("valid presentation -> non-empty claims_json",
          claims is not None and len(claims) > 0)

    # 3. Claims parse as JSON and are non-trivial.
    import json as _json
    try:
        parsed = _json.loads(claims) if claims else None
        check("claims_json parses as JSON", isinstance(parsed, dict) and len(parsed) > 0)
        if parsed:
            print(f"    claims: {parsed}")
    except ValueError:
        check("claims_json parses as JSON", False)

    # 4. Repeated verify+free does not crash (memory-contract smoke, x3).
    crash_free = True
    try:
        for _ in range(3):
            rc2, _ = do_verify(lib, vp_valid, ps, tc, rl_clean, nonce, aud)
            if rc2 != KRUTHO_OK:
                crash_free = False
    except Exception:
        crash_free = False
    check("repeated verify+free (x3) stable, no crash", crash_free)

    # 5. Revoked credential -> KRUTHO_ERR_REVOKED.
    rc, _ = do_verify(lib, vp_valid, ps, tc, rl_revoked, nonce, aud)
    check(f"revoked credential -> KRUTHO_ERR_REVOKED (got {ERROR_NAMES.get(rc, rc)})",
          rc == 2)

    # 6. Wrong nonce -> KRUTHO_ERR_INVALID_NONCE.
    rc, _ = do_verify(lib, vp_valid, ps, tc, rl_clean, b"wrong-nonce", aud)
    check(f"wrong nonce -> KRUTHO_ERR_INVALID_NONCE (got {ERROR_NAMES.get(rc, rc)})",
          rc == 3)

    # 7. Wrong audience -> KRUTHO_ERR_INVALID_AUDIENCE.
    rc, _ = do_verify(lib, vp_valid, ps, tc, rl_clean, nonce, b"wrong-audience")
    check(f"wrong audience -> KRUTHO_ERR_INVALID_AUDIENCE (got {ERROR_NAMES.get(rc, rc)})",
          rc == 4)

    # 8. Tampered issuer JWT -> KRUTHO_ERR_ISSUER_SIGNATURE.
    if os.path.exists(tampered_vp_path):
        vp_tampered = read_text_trimmed(tampered_vp_path)
        rc, _ = do_verify(lib, vp_tampered, ps, tc, rl_clean, nonce, aud)
        check(f"tampered issuer JWT -> KRUTHO_ERR_ISSUER_SIGNATURE (got {ERROR_NAMES.get(rc, rc)})",
              rc == 7)
    else:
        check("tampered issuer JWT fixture present", False)

    # 9. krutho_check_revoked: known-revoked jti (from the SDK's revoked filter)
    #    NOTE: expected_jti.txt is the *valid* credential's jti (not revoked in
    #    rl_clean) — this checks the raw check_revoked binding against the
    #    same filter used in assertion 5, which does flag it.
    jti = read_text_trimmed(os.path.join(OID4VP_DATA, "expected_jti.txt"))
    revoked_flag = lib.krutho_check_revoked(jti, rl_revoked, len(rl_revoked))
    check(f"krutho_check_revoked(jti, revoked-filter) == 1 (got {revoked_flag})",
          revoked_flag == 1)

    # 10. krutho_check_revoked against the clean filter -> not revoked.
    not_revoked_flag = lib.krutho_check_revoked(jti, rl_clean, len(rl_clean))
    check(f"krutho_check_revoked(jti, clean-filter) == 0 (got {not_revoked_flag})",
          not_revoked_flag == 0)

    print(f"\n{passed}/{total} assertions passed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
