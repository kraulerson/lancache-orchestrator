namespace SteamManifestHelper;

public static class Cli
{
    private const string Usage =
        "usage: SteamManifestHelper fetch --apps <file> --out <dir> --session-dir <dir> --username <name> [--import-from <dir>]\n" +
        "       SteamManifestHelper login --username <name> --session-dir <dir>";

    public static async Task<int> RunAsync(string[] args, TextWriter stdout, TextWriter stderr, Func<ISteamGateway> gatewayFactory, CancellationToken ct)
    {
        var command = args.Length > 0 ? args[0] : "";
        var options = args.Length > 0 ? ParseOptions(args, 1) : null;
        string[] required = command == "fetch"
            ? ["--apps", "--out", "--session-dir", "--username"]
            : ["--username", "--session-dir"];
        if (command is not ("fetch" or "login") || options is null || required.Any(r => !options.ContainsKey(r)))
        {
            stderr.WriteLine(Usage);
            return ExitCodes.Usage;
        }
        try
        {
            return command == "fetch"
                ? await FetchAsync(options, stdout, stderr, gatewayFactory, ct)
                : await LoginCommand.RunAsync(options["--username"], options["--session-dir"], stderr, ct);
        }
        // Only the caller's own cancellation propagates; SteamKit2's job timeouts and
        // disconnect cancellations are also OperationCanceledException and must exit 1.
        catch (Exception e) when (e is not OperationCanceledException || !ct.IsCancellationRequested)
        {
            stderr.WriteLine($"unexpected: {e.GetType().Name}: {e.Message}");
            return ExitCodes.Unexpected;
        }
    }

    private static async Task<int> FetchAsync(Dictionary<string, string> options, TextWriter stdout, TextWriter stderr, Func<ISteamGateway> gatewayFactory, CancellationToken ct)
    {
        var username = options["--username"];
        var sessionDir = options["--session-dir"];
        SteamSession session;
        try
        {
            session = SessionStore.Load(sessionDir, username, options.GetValueOrDefault("--import-from"));
        }
        catch (SessionImportException e)
        {
            var reason = $"{e.Message}. Run: SteamManifestHelper login --username {username} --session-dir {sessionDir}";
            stderr.WriteLine(reason);
            ResultWriter.Write(stdout, new RunSummary(SessionStatus.ImportFailed, reason));
            return ExitCodes.ImportFailed;
        }
        var appIds = ReadAppIds(options["--apps"]);
        await using var gateway = gatewayFactory();
        var runner = new FetchRunner(gateway, stdout, stderr, (wait, token) => Task.Delay(wait, token));
        return await runner.RunAsync(session, appIds, options["--out"], ct);
    }

    internal static Dictionary<string, string>? ParseOptions(string[] args, int start)
    {
        var options = new Dictionary<string, string>();
        for (var i = start; i < args.Length; i += 2)
        {
            if (!args[i].StartsWith("--", StringComparison.Ordinal) || i + 1 >= args.Length)
            {
                return null;
            }
            options[args[i]] = args[i + 1];
        }
        return options;
    }

    internal static IReadOnlyList<uint> ReadAppIds(string path) =>
        File.ReadLines(path).Select(l => l.Trim()).Where(l => l.Length > 0).Select(uint.Parse).Distinct().ToList();
}
