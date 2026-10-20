Extra root CAs for the Docker build (`*.crt`, PEM). Needed when something on
the build machine intercepts HTTPS (here: Avast Web/Mail Shield), otherwise
`npm ci` and `pip install` fail certificate checks inside the container.

Export on Windows (PowerShell), replacing the subject as needed:

    $c = Get-ChildItem Cert:\LocalMachine\Root | ? Subject -like "*Avast Web/Mail Shield Root*"
    "-----BEGIN CERTIFICATE-----`n" + [Convert]::ToBase64String($c.RawData,'InsertLineBreaks') +
      "`n-----END CERTIFICATE-----" | Set-Content -Encoding ascii certs\avast-root.crt

The `.crt` files are gitignored: such a root is generated per machine.
