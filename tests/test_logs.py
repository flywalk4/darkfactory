"""Check logs reach prompts without restore noise but with every failure."""

from darkfactory import logs


def test_noise_dropped_and_failures_kept():
    restore = [f"  Restored /workspace/src/p{i}/p{i}.csproj (in 1 ms)." for i in range(400)]
    passing = [f"  ok test_case_{i}" for i in range(3000)]
    text = "\n".join(
        [
            "$ dotnet test",
            *restore,
            *passing[:1500],
            "  FAIL test_branch_missing",
            "    Assert.Equal() Failure",
            *passing[1500:],
            "exit=1",
        ]
    )
    out = logs.condense(text, 8000)
    assert len(out) <= 8000
    assert "Restored /workspace" not in out
    assert "400 restore/progress/duplicate lines omitted" in out
    assert "FAIL test_branch_missing" in out and "Assert.Equal() Failure" in out
    assert out.rstrip().endswith("[darkfactory: 400 restore/progress/duplicate lines omitted]")
    assert "$ dotnet test" in out


def test_short_logs_unchanged():
    assert logs.condense("$ pytest\n1 failed\nexit=1") == "$ pytest\n1 failed\nexit=1"


def test_review_digests_drop_passing_output_and_lockfiles():
    checks = (
        "$ npm test\nexit=0\n"
        + "ok\n" * 500
        + "Tests: 113 passed\n\n$ dotnet test\nexit=1\n  FAIL Shop.Tests\n"
    )
    digest = logs.checks_digest(checks)
    assert digest.startswith("$ npm test\nexit=0\nTests: 113 passed")
    assert "FAIL Shop.Tests" in digest and len(digest) < 200
    lock = "diff --git a/web/package-lock.json b/web/package-lock.json\n--- a\n+++ b\n" + "+x\n" * 4000
    code = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n+print(1)\n"
    diff = logs.diff_digest(lock + code)
    assert "4000 changed lines of a lock/generated file omitted" in diff
    assert diff.endswith(code)


def test_review_diff_puts_code_first_and_scales_with_the_change():
    def file(path, lines):
        return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n" + "+x\n" * lines

    diff = (
        file("docs/reviews/report.md", 3000)
        + file("scripts/acceptance/run.cjs", 2000)
        + file("src/app/connector.ts", 40)
        + file("src/app/connector.spec.ts", 40)
        + file("src/backend/Executor.cs", 40)
    )
    digest = logs.diff_digest(diff, 8000)
    order = logs.diff_files(digest.split("[darkfactory:")[0])
    # Production code first, then tests; the writer's report and scripts do not push code out.
    assert order[:3] == ["src/app/connector.ts", "src/backend/Executor.cs", "src/app/connector.spec.ts"]
    assert "docs/reviews/report.md" in digest.split("[darkfactory:")[1]
    assert logs.review_budget("x" * 76000) == 76000
    assert logs.review_budget("x" * 10) == 32000 and logs.review_budget("x" * 900000) == 160000
    assert [
        logs.file_kind(p) for p in ("docs/a.md", "src/A.Tests/ATests.cs", "scripts/x.cjs", "src/a.ts")
    ] == [
        "document",
        "test",
        "script",
        "code",
    ]
