using System.Net;
using SteamKit2;
using SteamKit2.CDN;

namespace SteamManifestHelper;

/// <summary>ISteamGateway on SteamKit2 3.4.0, mirroring DepotDownloader 3.4.0's calls
/// (Steam3Session.cs, CDNClientPool.cs, ContentDownloader.cs:740-860). Its network
/// half is unproven live: the Task 1 spike ran scratch code making the same SteamKit2
/// calls, not this class, which first meets Steam at Task 10. Its pure helpers are
/// unit-tested (SteamKitGatewayTests, ConnectRetryTests).</summary>
public sealed class SteamKitGateway : ISteamGateway
{
    private static readonly TimeSpan StepTimeout = TimeSpan.FromSeconds(60);
    private const int MaxServersPerManifest = 6;
    private readonly TextWriter log;
    private readonly SteamClient client = new();
    private readonly CallbackManager callbacks;
    private readonly SteamUser user;
    private readonly SteamApps apps;
    private readonly SteamContent content;
    private readonly Client cdn;
    private readonly CancellationTokenSource pumpStop = new();
    private readonly Task pump;
    // Only non-null results are cached: a transient null for a widely borrowed owner app
    // (228980, the shared redistributables) must not poison every later borrowing app.
    private readonly Dictionary<uint, KeyValue> appInfo = new();
    private readonly Dictionary<(uint Depot, string Host), string> cdnTokens = new();
    private TaskCompletionSource connected = NewSignal();
    private TaskCompletionSource<EResult> loggedOn = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private volatile bool online;
    private List<Server>? servers;

    public SteamKitGateway(TextWriter log)
    {
        this.log = log;
        callbacks = new CallbackManager(client);
        user = client.GetHandler<SteamUser>()!;
        apps = client.GetHandler<SteamApps>()!;
        content = client.GetHandler<SteamContent>()!;
        cdn = new Client(client);
        callbacks.Subscribe<SteamClient.ConnectedCallback>(_ => connected.TrySetResult());
        callbacks.Subscribe<SteamClient.DisconnectedCallback>(OnDisconnected);
        callbacks.Subscribe<SteamUser.LoggedOnCallback>(OnLoggedOn);
        callbacks.Subscribe<SteamUser.LoggedOffCallback>(OnLoggedOff);
        pump = Task.Run(() =>
        {
            while (!pumpStop.IsCancellationRequested)
            {
                callbacks.RunWaitCallbacks(TimeSpan.FromMilliseconds(250));
            }
        });
    }

    private static TaskCompletionSource NewSignal() => new(TaskCreationOptions.RunContinuationsAsynchronously);

    /// <summary>`online` is set here, on the pump thread, so a disconnect or log-off the
    /// pump processes afterwards cannot be overwritten by the awaiting caller.</summary>
    private void OnLoggedOn(SteamUser.LoggedOnCallback cb)
    {
        online = cb.Result == EResult.OK;
        loggedOn.TrySetResult(cb.Result);
    }

    private void OnDisconnected(SteamClient.DisconnectedCallback cb)
    {
        online = false;
        var lost = new SessionLostException(cb.UserInitiated ? "disconnected by the helper" : "Steam closed the connection");
        connected.TrySetException(lost);
        loggedOn.TrySetException(lost);
    }

    /// <summary>Steam ended the session (LoggedInElsewhere, ...) without dropping the
    /// socket. That is a lost session, not a run of per-app timeouts.</summary>
    private void OnLoggedOff(SteamUser.LoggedOffCallback cb)
    {
        online = false;
        var lost = new SessionLostException($"Steam logged the session off: {cb.Result}");
        connected.TrySetException(lost);
        loggedOn.TrySetException(lost);
        // Close the socket now, so a late DisconnectedCallback cannot fail the reconnect's Connect().
        client.Disconnect();
    }

    /// <summary>Connect (up to 3 attempts, ConnectRetry), then log on exactly once. A
    /// connect that never succeeds, or a logon Steam never answers, is a network fault,
    /// not a refusal: it throws SessionLostException ("could not connect to Steam").
    /// Only Steam's own EResult answer becomes Refused or TokenRejected.</summary>
    public async Task<LogOnResult> ConnectAndLogOnAsync(SteamSession session, CancellationToken ct)
    {
        try
        {
            await ConnectRetry.RunAsync(async () =>
            {
                connected = NewSignal();
                loggedOn = new TaskCompletionSource<EResult>(TaskCreationOptions.RunContinuationsAsynchronously);
                try
                {
                    client.Connect();
                    await connected.Task.WaitAsync(StepTimeout, ct);
                }
                catch (Exception e) when (e is SessionLostException or TimeoutException)
                {
                    // Close a half-open attempt now, so its DisconnectedCallback lands during
                    // the wait and cannot fault the next attempt's fresh signal.
                    client.Disconnect();
                    log.WriteLine($"connect attempt failed: {(e is SessionLostException ? e.Message : "no answer within 60 s")}");
                    throw;
                }
            }, wait => Task.Delay(wait, ct));
            user.LogOn(new SteamUser.LogOnDetails
            {
                Username = session.Username,
                AccessToken = session.RefreshToken,
                ShouldRememberPassword = true,
                LoginID = 0x534D48, // "SMH": a fixed ID of the helper's own, as DepotDownloader sets its own.
            });
            var result = await loggedOn.Task.WaitAsync(StepTimeout, ct);
            if (result == EResult.OK)
            {
                log.WriteLine($"logged on as {session.Username}");
                return new LogOnResult(LogOnOutcome.Ok, "OK");
            }
            client.Disconnect();
            // The results DepotDownloader treats as "this token is dead" (Steam3Session.cs:594-599).
            return result is EResult.InvalidPassword or EResult.InvalidSignature or EResult.AccessDenied
                or EResult.Expired or EResult.Revoked
                ? new LogOnResult(LogOnOutcome.TokenRejected, result.ToString())
                : new LogOnResult(LogOnOutcome.Refused, result.ToString());
        }
        catch (Exception e) when (e is SessionLostException or TimeoutException)
        {
            client.Disconnect();
            throw new SessionLostException(
                $"could not connect to Steam: {(e is SessionLostException ? e.Message : "no answer within 60 s")}");
        }
    }

    private void EnsureOnline()
    {
        // `online` is only flipped when the pump runs OnDisconnected/OnLoggedOff, which can
        // lag the drop. SteamKit2's CMClient clears IsConnected before it posts the
        // DisconnectedCallback and before it cancels pending jobs, so checking it too
        // closes the window in which a cancelled job looks like a per-request failure.
        if (!online || !client.IsConnected)
        {
            throw new SessionLostException("not connected to Steam");
        }
    }

    /// <summary>Text for a failure that is safe to log and to hand to Python: the exception
    /// type and an HTTP status code, never e.Message. A web exception's message can carry
    /// the request URL, and a CDN URL can carry an auth token in its query string.</summary>
    internal static string Describe(Exception e) => e switch
    {
        SteamRequestException request => request.Message,
        SteamKitWebRequestException web => $"{nameof(SteamKitWebRequestException)}: HTTP {(int)web.StatusCode}",
        HttpRequestException { StatusCode: { } code } => $"{nameof(HttpRequestException)}: HTTP {(int)code}",
        _ => e.GetType().Name,
    };

    internal static HttpStatusCode? StatusOf(Exception e) => e switch
    {
        SteamKitWebRequestException web => web.StatusCode,
        HttpRequestException http => http.StatusCode,
        _ => null,
    };

    /// <summary>A timeout or an HTTP 5xx: the host is unwell, not the request.</summary>
    internal static bool IsSlowFailure(Exception e) =>
        e is TaskCanceledException or TimeoutException
        || e.InnerException is TimeoutException
        || (int?)StatusOf(e) >= 500;

    /// <summary>A failure while still online is one bad request; a failure after the
    /// connection dropped is a lost session. The re-check in the catch uses
    /// client.IsConnected as well as `online`, because the pump may not have processed
    /// the disconnect yet when SteamKit2 cancels the pending job.</summary>
    private async Task<T> Call<T>(Func<Task<T>> call, string what, CancellationToken ct)
    {
        EnsureOnline();
        try
        {
            return await call().WaitAsync(StepTimeout, ct);
        }
        catch (Exception e) when (!ct.IsCancellationRequested && e is not SessionLostException)
        {
            EnsureOnline();
            throw new SteamRequestException($"{what}: {Describe(e)}");
        }
    }

    public async Task<KeyValue?> GetAppInfoAsync(uint appId, CancellationToken ct)
    {
        if (appInfo.TryGetValue(appId, out var cached))
        {
            return cached;
        }
        var tokens = await Call(() => apps.PICSGetAccessTokens([appId], []).ToTask(), $"access token for app {appId}", ct);
        var request = new SteamApps.PICSRequest(appId);
        if (tokens.AppTokens.TryGetValue(appId, out var token))
        {
            request.AccessToken = token;
        }
        var info = await Call(() => apps.PICSGetProductInfo([request], []).ToTask(), $"app info for {appId}", ct);
        KeyValue? keyValues = null;
        foreach (var part in info.Results ?? [])
        {
            if (part.Apps.TryGetValue(appId, out var app))
            {
                keyValues = app.KeyValues;
            }
        }
        if (keyValues is not null)
        {
            appInfo[appId] = keyValues;
        }
        return keyValues;
    }

    public async Task<byte[]?> GetDepotKeyAsync(uint depotId, uint appId, CancellationToken ct)
    {
        var key = await Call(() => apps.GetDepotDecryptionKey(depotId, appId).ToTask(), $"depot key {depotId}", ct);
        return MapDepotKey(depotId, key.Result, key.DepotKey);
    }

    internal static byte[]? MapDepotKey(uint depotId, EResult result, byte[] key) => result switch
    {
        EResult.OK => key,
        // Only AccessDenied means "this account does not own the depot". Busy,
        // RateLimitExceeded and the like are transient failures, never not_owned.
        EResult.AccessDenied => null,
        _ => throw new SteamRequestException($"depot key {depotId}: {result}"),
    };

    public async Task SaveManifestAsync(uint depotId, uint containingAppId, ulong manifestId, byte[] depotKey, string path, CancellationToken ct)
    {
        if (servers is null)
        {
            var fetched = (await Call(() => content.GetServersForSteamPipe(), "CDN server list", ct))
                .Where(s => s.Type is "SteamCache" or "CDN")
                .OrderBy(s => s.WeightedLoad)
                .ToList();
            if (fetched.Count == 0)
            {
                // Not cached: one empty answer must not fail every later manifest in the run.
                throw new SteamRequestException("Steam returned no CDN servers");
            }
            servers = fetched;
        }
        var code = await Call(() => content.GetManifestRequestCode(depotId, containingAppId, manifestId, "public"),
            $"request code for depot {depotId}", ct);
        if (code == 0)
        {
            throw new SteamRequestException($"no manifest request code for depot {depotId}");
        }

        Exception? last = null;
        // Materialized: the loop below reorders `servers`, which must not be the collection being enumerated.
        var candidates = servers
            .Where(s => s.AllowedAppIds.Length == 0 || s.AllowedAppIds.Contains(containingAppId))
            .Take(MaxServersPerManifest)
            .ToList();
        foreach (var server in candidates)
        {
            var host = server.Host!;
            cdnTokens.TryGetValue((depotId, host), out var known);
            try
            {
                var manifest = await CdnAuth.WithAuthRetryAsync(
                    token => cdn.DownloadManifestAsync(depotId, manifestId, code, server, depotKey, null, token),
                    known,
                    async () =>
                    {
                        var auth = await Call(() => content.GetCDNAuthToken(containingAppId, depotId, host), $"CDN token for {host}", ct);
                        if (auth.Result != EResult.OK || auth.Token is null)
                        {
                            return null;
                        }
                        cdnTokens[(depotId, host)] = auth.Token;
                        return auth.Token;
                    });
                manifest.SaveToFile(path);
                return;
            }
            catch (Exception e) when (!ct.IsCancellationRequested && e is not SessionLostException)
            {
                EnsureOnline();
                last = e;
                // DepotDownloader aborts on these (ContentDownloader.cs:825-835): another server
                // will not change the answer. A 403 reaching here already survived CdnAuth's retry.
                if (StatusOf(e) is HttpStatusCode.Unauthorized or HttpStatusCode.NotFound or HttpStatusCode.Forbidden)
                {
                    break;
                }
                // A hung or failing host would otherwise cost its timeout on every manifest
                // of the run, because the list is ordered once. Send it to the back.
                if (IsSlowFailure(e))
                {
                    servers.Remove(server);
                    servers.Add(server);
                }
            }
        }
        throw new SteamRequestException($"manifest {manifestId} for depot {depotId}: {(last is null ? "no CDN server available" : Describe(last))}");
    }

    public async ValueTask DisposeAsync()
    {
        if (online)
        {
            user.LogOff();
        }
        client.Disconnect();
        pumpStop.Cancel();
        await pump;
    }
}
