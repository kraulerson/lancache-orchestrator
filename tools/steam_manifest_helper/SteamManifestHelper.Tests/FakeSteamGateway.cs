using SteamKit2;
using SteamManifestHelper;

namespace SteamManifestHelper.Tests;

internal sealed class FakeSteamGateway : ISteamGateway
{
    /// <summary>Consulted before LogOnResults, one entry per logon call: an exception is thrown, null falls through.</summary>
    public readonly Queue<Exception?> LogOnThrows = new();
    public readonly Queue<LogOnResult> LogOnResults = new();
    public readonly Dictionary<uint, KeyValue?> Apps = new();
    public readonly HashSet<uint> DepotsWithoutAccess = new();
    public readonly HashSet<uint> UnavailableDepots = new();
    /// <summary>app id -> how many more times asking for its info drops the connection.</summary>
    public readonly Dictionary<uint, int> DropsOnApp = new();
    public readonly List<(uint Depot, uint ContainingApp, ulong Manifest)> Saved = new();
    public int LogOnCalls { get; private set; }

    public Task<LogOnResult> ConnectAndLogOnAsync(SteamSession session, CancellationToken ct)
    {
        LogOnCalls++;
        if (LogOnThrows.Count > 0 && LogOnThrows.Dequeue() is { } toThrow)
        {
            throw toThrow;
        }
        return Task.FromResult(LogOnResults.Count > 0 ? LogOnResults.Dequeue() : new LogOnResult(LogOnOutcome.Ok, "OK"));
    }

    public Task<KeyValue?> GetAppInfoAsync(uint appId, CancellationToken ct)
    {
        if (DropsOnApp.TryGetValue(appId, out var drops) && drops > 0)
        {
            DropsOnApp[appId] = drops - 1;
            throw new SessionLostException("NoConnection");
        }
        return Task.FromResult(Apps.TryGetValue(appId, out var info) ? info : null);
    }

    public Task<byte[]?> GetDepotKeyAsync(uint depotId, uint appId, CancellationToken ct) =>
        Task.FromResult<byte[]?>(DepotsWithoutAccess.Contains(depotId) ? null : [1, 2, 3]);

    public Task SaveManifestAsync(uint depotId, uint containingAppId, ulong manifestId, byte[] depotKey, string path, CancellationToken ct)
    {
        if (UnavailableDepots.Contains(depotId))
        {
            throw new SteamRequestException("CDN returned 404");
        }
        Saved.Add((depotId, containingAppId, manifestId));
        File.WriteAllBytes(path, [0xD0, 0x17, 0xF6, 0x71]);
        return Task.CompletedTask;
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}
