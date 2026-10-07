using System.Text.Json;
using SteamKit2;
using SteamManifestHelper;
using Xunit;

namespace SteamManifestHelper.Tests;

public sealed class FetchRunnerTests : IDisposable
{
    private const string Token = "eyJ-test-refresh-token-0123456789";
    private static readonly SteamSession Session = new("kraulerson", Token);
    private readonly string outDir = Directory.CreateTempSubdirectory("smh-out-").FullName;
    private readonly StringWriter results = new();
    private readonly StringWriter log = new();
    private readonly List<TimeSpan> delays = new();

    public void Dispose() => Directory.Delete(outDir, recursive: true);

    private static KeyValue App(string depotsBody, string common = "") =>
        KeyValue.LoadFromString($"\"appinfo\"\n{{\n\"common\"\n{{\n{common}\n}}\n\"depots\"\n{{\n{depotsBody}\n}}\n}}")!;

    private static string Depot(uint id, ulong gid) =>
        $"\"{id}\" {{ \"config\" {{ \"oslist\" \"windows\" }} \"manifests\" {{ \"public\" {{ \"gid\" \"{gid}\" }} }} }}";

    private FetchRunner Runner(FakeSteamGateway gateway) =>
        new(gateway, results, log, (wait, _) => { delays.Add(wait); return Task.CompletedTask; });

    private List<JsonElement> Lines() =>
        results.ToString().Split('\n', StringSplitOptions.RemoveEmptyEntries)
            .Select(l => JsonDocument.Parse(l).RootElement).ToList();

    private static FakeSteamGateway ThreeGoodApps()
    {
        var gateway = new FakeSteamGateway();
        gateway.Apps[10] = App(Depot(11, 111));
        gateway.Apps[20] = App(Depot(21, 222));
        gateway.Apps[30] = App(Depot(31, 333));
        return gateway;
    }

    [Fact]
    public async Task Logs_on_once_for_the_whole_run()
    {
        var gateway = ThreeGoodApps();
        var code = await Runner(gateway).RunAsync(Session, [10, 20, 30], outDir, CancellationToken.None);
        Assert.Equal(ExitCodes.Completed, code);
        Assert.Equal(1, gateway.LogOnCalls);
        var lines = Lines();
        Assert.Equal(new[] { "ok", "ok", "ok" }, lines.Take(3).Select(l => l.GetProperty("status").GetString()));
        Assert.Equal("11_111.manifest", lines[0].GetProperty("manifests")[0].GetString());
        Assert.True(File.Exists(Path.Combine(outDir, "10", "11_111.manifest")));
        Assert.True(lines[^1].GetProperty("summary").GetBoolean());
        Assert.Equal("completed", lines[^1].GetProperty("session").GetString());
    }

    [Fact]
    public async Task A_refused_login_stops_at_once_with_steams_reason()
    {
        var gateway = ThreeGoodApps();
        gateway.LogOnResults.Enqueue(new LogOnResult(LogOnOutcome.Refused, "RateLimitExceeded"));
        var code = await Runner(gateway).RunAsync(Session, [10, 20, 30], outDir, CancellationToken.None);
        Assert.Equal(ExitCodes.LoginRefused, code);
        Assert.Equal(1, gateway.LogOnCalls);
        var only = Assert.Single(Lines());
        Assert.Equal("login_refused", only.GetProperty("session").GetString());
        Assert.Contains("RateLimitExceeded", only.GetProperty("reason").GetString());
    }

    [Fact]
    public async Task A_rejected_token_names_the_login_command()
    {
        var gateway = ThreeGoodApps();
        gateway.LogOnResults.Enqueue(new LogOnResult(LogOnOutcome.TokenRejected, "Expired"));
        var code = await Runner(gateway).RunAsync(Session, [10], outDir, CancellationToken.None);
        Assert.Equal(ExitCodes.LoginRefused, code);
        Assert.Contains("login --username kraulerson", Lines().Single().GetProperty("reason").GetString());
    }

    [Fact]
    public async Task One_drop_reconnects_once_and_retries_the_interrupted_app()
    {
        var gateway = ThreeGoodApps();
        gateway.DropsOnApp[20] = 1;
        var code = await Runner(gateway).RunAsync(Session, [10, 20, 30], outDir, CancellationToken.None);
        Assert.Equal(ExitCodes.Completed, code);
        Assert.Equal(2, gateway.LogOnCalls);
        Assert.Equal(new[] { FetchRunner.ReconnectWait }, delays);
        Assert.Equal(new[] { "ok", "ok", "ok" }, Lines().Take(3).Select(l => l.GetProperty("status").GetString()));
    }

    [Fact]
    public async Task A_second_drop_stops_and_marks_the_rest_not_attempted()
    {
        var gateway = ThreeGoodApps();
        gateway.DropsOnApp[20] = 2;
        var code = await Runner(gateway).RunAsync(Session, [10, 20, 30], outDir, CancellationToken.None);
        Assert.Equal(ExitCodes.Disconnected, code);
        Assert.Equal(2, gateway.LogOnCalls);
        var lines = Lines();
        Assert.Equal(new[] { "ok", "not_attempted", "not_attempted" }, lines.Take(3).Select(l => l.GetProperty("status").GetString()));
        Assert.Equal("disconnected", lines[^1].GetProperty("session").GetString());
    }

    [Fact]
    public async Task A_refused_reconnect_stops_as_disconnected()
    {
        var gateway = ThreeGoodApps();
        gateway.DropsOnApp[20] = 1;
        gateway.LogOnResults.Enqueue(new LogOnResult(LogOnOutcome.Ok, "OK"));
        gateway.LogOnResults.Enqueue(new LogOnResult(LogOnOutcome.Refused, "RateLimitExceeded"));
        var code = await Runner(gateway).RunAsync(Session, [10, 20, 30], outDir, CancellationToken.None);
        Assert.Equal(ExitCodes.Disconnected, code);
        Assert.Contains("RateLimitExceeded", Lines()[^1].GetProperty("reason").GetString());
    }

    [Fact]
    public async Task An_app_steam_knows_nothing_about_is_an_error_and_the_run_continues()
    {
        var gateway = ThreeGoodApps();
        gateway.Apps.Remove(20);
        var code = await Runner(gateway).RunAsync(Session, [10, 20, 30], outDir, CancellationToken.None);
        Assert.Equal(ExitCodes.Completed, code);
        Assert.Equal(new[] { "ok", "error", "ok" }, Lines().Take(3).Select(l => l.GetProperty("status").GetString()));
    }

    [Fact]
    public async Task An_app_the_account_does_not_own_is_not_owned_and_the_run_continues()
    {
        // Review Focus 2.
        var gateway = ThreeGoodApps();
        gateway.DepotsWithoutAccess.Add(21);
        var code = await Runner(gateway).RunAsync(Session, [10, 20, 30], outDir, CancellationToken.None);
        Assert.Equal(ExitCodes.Completed, code);
        Assert.Equal(new[] { "ok", "not_owned", "ok" }, Lines().Take(3).Select(l => l.GetProperty("status").GetString()));
        Assert.Equal(1, gateway.LogOnCalls);
    }

    [Fact]
    public async Task An_app_with_no_windows_depot_is_no_depots()
    {
        var gateway = new FakeSteamGateway();
        gateway.Apps[10] = App("\"11\" { \"config\" { \"oslist\" \"linux\" } \"manifests\" { \"public\" { \"gid\" \"1\" } } }");
        await Runner(gateway).RunAsync(Session, [10], outDir, CancellationToken.None);
        Assert.Equal("no_depots", Lines()[0].GetProperty("status").GetString());
    }

    [Fact]
    public async Task A_borrowed_depot_is_fetched_through_the_owning_app()
    {
        // Review Focus 1.
        var gateway = new FakeSteamGateway();
        gateway.Apps[10] = App("\"228988\" { \"depotfromapp\" \"228980\" }");
        gateway.Apps[228980] = App(Depot(228988, 4242));
        await Runner(gateway).RunAsync(Session, [10], outDir, CancellationToken.None);
        var line = Lines()[0];
        Assert.Equal("ok", line.GetProperty("status").GetString());
        Assert.Equal("228988_4242.manifest", line.GetProperty("manifests")[0].GetString());
        Assert.Equal((228988u, 228980u, 4242ul), gateway.Saved.Single());
    }

    [Fact]
    public async Task An_unavailable_manifest_is_an_error_with_its_reason()
    {
        var gateway = ThreeGoodApps();
        gateway.UnavailableDepots.Add(21);
        await Runner(gateway).RunAsync(Session, [10, 20, 30], outDir, CancellationToken.None);
        var failed = Lines()[1];
        Assert.Equal("error", failed.GetProperty("status").GetString());
        Assert.Contains("404", failed.GetProperty("reason").GetString());
    }

    [Fact]
    public async Task The_token_appears_in_no_output()
    {
        var gateway = ThreeGoodApps();
        gateway.LogOnResults.Enqueue(new LogOnResult(LogOnOutcome.TokenRejected, "Expired"));
        await Runner(gateway).RunAsync(Session, [10], outDir, CancellationToken.None);
        Assert.DoesNotContain(Token, results.ToString());
        Assert.DoesNotContain(Token, log.ToString());
    }
}
