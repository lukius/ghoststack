import httpx


def test_httpx_does_not_trust_distrusted_emsign_root():
    roots = httpx.create_ssl_context().get_ca_certs()
    names = {
        dict(rdn[0] for rdn in cert["subject"]).get("commonName") for cert in roots
    }

    assert names, "httpx loaded an empty trust store"
    assert "emSign ECC Root CA - G3" not in names
