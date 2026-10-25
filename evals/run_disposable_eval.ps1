$ErrorActionPreference = "Stop"

function New-EvaluationSecret {
    $bytes = New-Object byte[] 32
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
        return [BitConverter]::ToString($bytes).Replace("-", "")
    }
    finally {
        $generator.Dispose()
    }
}

$env:EVAL_DB_PASSWORD = New-EvaluationSecret
$env:EVAL_SECRET_KEY = New-EvaluationSecret
$env:EVAL_SMS_GATEWAY_TOKEN = New-EvaluationSecret
$env:EVAL_ADMIN_USERNAME = "eval-admin"
$env:EVAL_ADMIN_PASSWORD = New-EvaluationSecret

$composeArgs = @(
    "-f", "docker-compose.eval.yml",
    "-p", "sewasetu-eval"
)
$evaluationExitCode = 1

try {
    & docker compose @composeArgs up -d --build eval-db eval-smsgw eval-app eval-citizen-mcp eval-officer-mcp
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to start the isolated evaluation services."
    }

    & docker compose @composeArgs run --build --rm eval-runner
    $evaluationExitCode = $LASTEXITCODE
    if ($evaluationExitCode -ne 0) {
        & docker compose @composeArgs logs --no-color --tail 100 eval-app eval-db
    }
}
finally {
    & docker compose @composeArgs down --volumes --remove-orphans
    if ($LASTEXITCODE -ne 0 -and $evaluationExitCode -eq 0) {
        $evaluationExitCode = 1
    }

    Remove-Item Env:EVAL_DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:EVAL_SECRET_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:EVAL_SMS_GATEWAY_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:EVAL_ADMIN_USERNAME -ErrorAction SilentlyContinue
    Remove-Item Env:EVAL_ADMIN_PASSWORD -ErrorAction SilentlyContinue
}

exit $evaluationExitCode
