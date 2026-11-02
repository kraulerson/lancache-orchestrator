namespace SteamManifestHelper;

/// <summary>One login for the whole run, one JSON line per app, and at most one
/// reconnect. A logon loop is what #361 was; this class must never make one.</summary>
public sealed class FetchRunner(ISteamGateway gateway, TextWriter results, TextWriter log, Func<TimeSpan, CancellationToken, Task> delay)
{
    public static readonly TimeSpan ReconnectWait = TimeSpan.FromSeconds(60);

    /// <summary>Calls to ConnectAndLogOnAsync, reported in every summary as "logons" so
    /// a live run can be checked for the one-login rule. A call whose connect failed
    /// before LogOn was sent still counts: this is an upper bound, never an undercount.</summary>
    private int logons;

    private Task<LogOnResult> LogOnAsync(SteamSession session, CancellationToken ct)
    {
        logons++;
        return gateway.ConnectAndLogOnAsync(session, ct);
    }

    public async Task<int> RunAsync(SteamSession session, IReadOnlyList<uint> appIds, string outDir, CancellationToken ct)
    {
        LogOnResult logon;
        try
        {
            logon = await LogOnAsync(session, ct);
        }
        catch (Exception e) when (e is SessionLostException or SteamRequestException)
        {
            // The token was never judged, so no "run login" advice here.
            var unreachable = $"could not reach Steam: {e.Message}";
            log.WriteLine(unreachable);
            ResultWriter.Write(results, new RunSummary(SessionStatus.LoginRefused, unreachable, logons));
            return ExitCodes.LoginRefused;
        }
        if (logon.Outcome != LogOnOutcome.Ok)
        {
            var reason = logon.Outcome == LogOnOutcome.TokenRejected
                ? $"Steam rejected the saved login ({logon.Detail}). Run: SteamManifestHelper login --username {session.Username} --session-dir <session dir>"
                : $"Steam refused the login: {logon.Detail}";
            log.WriteLine(reason);
            ResultWriter.Write(results, new RunSummary(SessionStatus.LoginRefused, reason, logons));
            return ExitCodes.LoginRefused;
        }
        log.WriteLine($"logged on as {session.Username}; fetching {appIds.Count} apps");

        var reconnected = false;
        var index = 0;
        while (index < appIds.Count)
        {
            try
            {
                ResultWriter.Write(results, await FetchAppAsync(appIds[index], outDir, ct));
                index++;
            }
            catch (SessionLostException lost)
            {
                log.WriteLine($"connection lost at app {appIds[index]}: {lost.Message}");
                if (reconnected)
                {
                    return StopEarly(appIds, index, $"connection lost twice: {lost.Message}");
                }
                reconnected = true;
                await delay(ReconnectWait, ct);
                LogOnResult again;
                try
                {
                    again = await LogOnAsync(session, ct);
                }
                catch (Exception e) when (e is SessionLostException or SteamRequestException)
                {
                    return StopEarly(appIds, index, $"reconnect failed: {e.Message}");
                }
                if (again.Outcome != LogOnOutcome.Ok)
                {
                    return StopEarly(appIds, index, $"reconnect refused: {again.Detail}");
                }
                log.WriteLine("reconnected; retrying the interrupted app");
            }
        }
        ResultWriter.Write(results, new RunSummary(SessionStatus.Completed, "", logons));
        return ExitCodes.Completed;
    }

    private int StopEarly(IReadOnlyList<uint> appIds, int from, string reason)
    {
        for (var i = from; i < appIds.Count; i++)
        {
            ResultWriter.Write(results, new AppResult(appIds[i], AppStatus.NotAttempted, Reason: reason));
        }
        log.WriteLine(reason);
        ResultWriter.Write(results, new RunSummary(SessionStatus.Disconnected, reason, logons));
        return ExitCodes.Disconnected;
    }

    private async Task<AppResult> FetchAppAsync(uint appId, string outDir, CancellationToken ct)
    {
        try
        {
            var info = await gateway.GetAppInfoAsync(appId, ct);
            if (info is null)
            {
                return new AppResult(appId, AppStatus.Error, Reason: "Steam returned no app info");
            }
            var depots = info["depots"];
            var selected = DepotSelector.SelectDepots(depots);
            if (selected.Count == 0)
            {
                return new AppResult(appId, AppStatus.NoDepots, Reason: "no Windows 64-bit English depots");
            }

            var saved = new List<string>();
            var noAccess = 0;
            foreach (var depotId in selected)
            {
                var lookup = DepotSelector.ResolveManifest(depots, depotId, appId);
                if (lookup.RedirectAppId != 0)
                {
                    var owner = await gateway.GetAppInfoAsync(lookup.RedirectAppId, ct);
                    if (owner is null)
                    {
                        throw new SteamRequestException($"Steam returned no app info for app {lookup.RedirectAppId}, owner of depot {depotId}");
                    }
                    lookup = DepotSelector.ResolveManifest(owner["depots"], depotId, lookup.RedirectAppId);
                }
                if (lookup.ManifestId == 0)
                {
                    continue;
                }
                var key = await gateway.GetDepotKeyAsync(depotId, appId, ct);
                if (key is null)
                {
                    noAccess++;
                    continue;
                }
                var name = $"{depotId}_{lookup.ManifestId}.manifest";
                var appDir = Path.Combine(outDir, appId.ToString());
                Directory.CreateDirectory(appDir);
                var containing = DepotSelector.ContainingAppId(info, depotId, appId);
                await gateway.SaveManifestAsync(depotId, containing, lookup.ManifestId, key, Path.Combine(appDir, name), ct);
                saved.Add(name);
            }

            if (saved.Count > 0)
            {
                return new AppResult(appId, AppStatus.Ok, Manifests: saved);
            }
            return noAccess > 0
                ? new AppResult(appId, AppStatus.NotOwned, Reason: $"no access to {noAccess} depot(s)")
                : new AppResult(appId, AppStatus.NoDepots, Reason: "no public manifest for any selected depot");
        }
        catch (SteamRequestException e)
        {
            return new AppResult(appId, AppStatus.Error, Reason: e.Message);
        }
    }
}
