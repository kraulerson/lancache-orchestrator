using SteamKit2;

namespace SteamManifestHelper;

public enum LogOnOutcome { Ok, TokenRejected, Refused }

public sealed record LogOnResult(LogOnOutcome Outcome, string Detail);

/// <summary>The connection to Steam dropped. Only FetchRunner handles it.</summary>
public sealed class SessionLostException(string message) : Exception(message);

/// <summary>One request failed while the session is still up (no request code,
/// CDN 401/404, a timeout...). The app it belongs to is reported as an error.</summary>
public sealed class SteamRequestException(string message) : Exception(message);

public interface ISteamGateway : IAsyncDisposable
{
    /// <summary>Connect, and log on ONCE with the session's token. Never retries.</summary>
    Task<LogOnResult> ConnectAndLogOnAsync(SteamSession session, CancellationToken ct);

    /// <summary>The app's PICS key/values, or null when Steam returns none.</summary>
    Task<KeyValue?> GetAppInfoAsync(uint appId, CancellationToken ct);

    /// <summary>The depot decryption key, or null when this account has no access.</summary>
    Task<byte[]?> GetDepotKeyAsync(uint depotId, uint appId, CancellationToken ct);

    /// <summary>Download one manifest and write it with DepotManifest.SaveToFile.</summary>
    Task SaveManifestAsync(uint depotId, uint containingAppId, ulong manifestId, byte[] depotKey, string path, CancellationToken ct);
}
