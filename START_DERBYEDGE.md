# Start DerbyEdge Engine

Canonical local startup procedure for the DerbyEdge Streamlit operator console.

## Prerequisites

- Run commands from the DerbyEdge repository root.
- Use the repository-managed Python virtual environment at `.venv`.
- Dependencies are locked in `requirements.lock`.

## Start the application

In PowerShell:

```powershell
cd <DerbyEdge-repository>
.\.venv\Scripts\Activate.ps1
streamlit run src/app/app.py
```

Open the operator console at:

```text
http://127.0.0.1:8501
```

## Startup verification

Confirm all of the following before treating the session as operational:

1. The Streamlit server remains running without a fatal exception.
2. The browser loads the SAR R13 operator console.
3. The browser console has no errors.
4. No tracked source files were changed solely to start the app.

## Browser-agent prompt

```text
Start DerbyEdge Engine locally.

From the repository root:
1. Activate .venv.
2. Run: streamlit run src/app/app.py
3. Open http://127.0.0.1:8501 in the browser.
4. Confirm the SAR R13 operator console renders and report any fatal server or browser-console error.
5. Do not modify files unless a startup-blocking error occurs.
```

## Known non-blocking messages

Existing Streamlit deprecation and Arrow-conversion recovery warnings are non-blocking when the operator console renders, the server has no fatal runtime error, and the browser console is clean. Capture the exact warning text before making a warning-remediation change.
