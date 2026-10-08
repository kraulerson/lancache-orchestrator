using System.Text.Json;
using SteamManifestHelper;
using Xunit;

namespace SteamManifestHelper.Tests;

public sealed class CliTests : IDisposable
{
    private readonly string root = Directory.CreateTempSubdirectory("smh-cli-").FullName;

    public void Dispose() => Directory.Delete(root, recursive: true);

    [Fact]
    public async Task No_arguments_is_a_usage_error()
    {
        var stderr = new StringWriter();
        var code = await Cli.RunAsync([], new StringWriter(), stderr, () => new FakeSteamGateway(), CancellationToken.None);
        Assert.Equal(ExitCodes.Usage, code);
        Assert.Contains("usage", stderr.ToString(), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task A_missing_required_option_is_a_usage_error()
    {
        var code = await Cli.RunAsync(["fetch", "--apps", "x"], new StringWriter(), new StringWriter(), () => new FakeSteamGateway(), CancellationToken.None);
        Assert.Equal(ExitCodes.Usage, code);
    }

    [Fact]
    public async Task Fetch_without_any_session_exits_3_and_names_the_login_command()
    {
        var apps = Path.Combine(root, "apps.txt");
        File.WriteAllText(apps, "10\n");
        var stdout = new StringWriter();
        var code = await Cli.RunAsync(
            ["fetch", "--apps", apps, "--out", Path.Combine(root, "out"), "--session-dir", Path.Combine(root, "s"), "--username", "kraulerson", "--import-from", Path.Combine(root, "nope")],
            stdout, new StringWriter(), () => new FakeSteamGateway(), CancellationToken.None);
        Assert.Equal(ExitCodes.ImportFailed, code);
        var summary = JsonDocument.Parse(stdout.ToString().Trim()).RootElement;
        Assert.Equal("import_failed", summary.GetProperty("session").GetString());
        Assert.Contains("login --username kraulerson", summary.GetProperty("reason").GetString());
    }

    [Fact]
    public async Task Fetch_with_a_saved_session_runs_the_fetch()
    {
        var sessionDir = Path.Combine(root, "s");
        SessionStore.Save(sessionDir, new SteamSession("kraulerson", "eyJ-test"));
        var apps = Path.Combine(root, "apps.txt");
        File.WriteAllText(apps, "10\n");
        var gateway = new FakeSteamGateway();
        var code = await Cli.RunAsync(
            ["fetch", "--apps", apps, "--out", Path.Combine(root, "out"), "--session-dir", sessionDir, "--username", "kraulerson"],
            new StringWriter(), new StringWriter(), () => gateway, CancellationToken.None);
        Assert.Equal(ExitCodes.Completed, code);
        Assert.Equal(1, gateway.LogOnCalls);
    }

    [Fact]
    public async Task An_internal_cancellation_is_exit_1_not_a_crash()
    {
        var sessionDir = Path.Combine(root, "s");
        SessionStore.Save(sessionDir, new SteamSession("kraulerson", "eyJ-test"));
        var apps = Path.Combine(root, "apps.txt");
        File.WriteAllText(apps, "10\n");
        var gateway = new FakeSteamGateway();
        gateway.LogOnThrows.Enqueue(new TaskCanceledException("steam job timed out"));
        var stderr = new StringWriter();
        var code = await Cli.RunAsync(
            ["fetch", "--apps", apps, "--out", Path.Combine(root, "out"), "--session-dir", sessionDir, "--username", "kraulerson"],
            new StringWriter(), stderr, () => gateway, CancellationToken.None);
        Assert.Equal(ExitCodes.Unexpected, code);
        Assert.Contains("unexpected", stderr.ToString());
    }

    [Fact]
    public async Task An_unexpected_failure_prints_its_type_but_never_its_message()
    {
        var sessionDir = Path.Combine(root, "s");
        SessionStore.Save(sessionDir, new SteamSession("kraulerson", "eyJ-test"));
        var apps = Path.Combine(root, "apps.txt");
        File.WriteAllText(apps, "10\n");
        var gateway = new FakeSteamGateway();
        gateway.LogOnThrows.Enqueue(new InvalidOperationException("GET https://x/y?token=SECRET failed"));
        var stderr = new StringWriter();
        var stdout = new StringWriter();
        var code = await Cli.RunAsync(
            ["fetch", "--apps", apps, "--out", Path.Combine(root, "out"), "--session-dir", sessionDir, "--username", "kraulerson"],
            stdout, stderr, () => gateway, CancellationToken.None);
        Assert.Equal(ExitCodes.Unexpected, code);
        Assert.Contains("unexpected: InvalidOperationException", stderr.ToString());
        Assert.DoesNotContain("SECRET", stderr.ToString());
        Assert.DoesNotContain("SECRET", stdout.ToString());
    }

    [Fact]
    public async Task A_failed_import_summary_reports_zero_logons()
    {
        var apps = Path.Combine(root, "apps.txt");
        File.WriteAllText(apps, "10\n");
        var stdout = new StringWriter();
        await Cli.RunAsync(
            ["fetch", "--apps", apps, "--out", Path.Combine(root, "out"), "--session-dir", Path.Combine(root, "s"), "--username", "kraulerson", "--import-from", Path.Combine(root, "nope")],
            stdout, new StringWriter(), () => new FakeSteamGateway(), CancellationToken.None);
        Assert.Equal(0, JsonDocument.Parse(stdout.ToString().Trim()).RootElement.GetProperty("logons").GetInt32());
    }

    [Fact]
    public void App_ids_are_read_once_each_and_blank_lines_are_skipped()
    {
        var apps = Path.Combine(root, "apps.txt");
        File.WriteAllText(apps, "10\n\n20\n10\n 30 \n");
        Assert.Equal(new uint[] { 10, 20, 30 }, Cli.ReadAppIds(apps));
    }
}
