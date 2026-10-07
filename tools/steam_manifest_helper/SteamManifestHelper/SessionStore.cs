using System.IO.Compression;
using System.Text.Json;
using ProtoBuf;

namespace SteamManifestHelper;

/// <summary>The one credential the helper holds: a Steam refresh token for one account.</summary>
public sealed record SteamSession(string Username, string RefreshToken)
{
    // A record's generated ToString prints every property. Never the token.
    public override string ToString() => $"SteamSession {{ Username = {Username} }}";
}

/// <summary>No usable session could be loaded or imported (exit code 3).</summary>
public sealed class SessionImportException(string message) : Exception(message);

/// <summary>Mirror of DepotDownloader 3.4.0's AccountSettingsStore. Only field 4,
/// LoginTokens (username to refresh token), is read; protobuf skips the rest.</summary>
[ProtoContract]
internal sealed class DepotDownloaderAccountStore
{
    [ProtoMember(4, IsRequired = false)]
    public Dictionary<string, string> LoginTokens { get; set; } = new();
}

public static class SessionStore
{
    public const string FileName = "session.json";

    /// <summary>Load this helper's own session, importing it once from
    /// DepotDownloader's store when none exists yet.</summary>
    public static SteamSession Load(string sessionDir, string username, string? importFrom)
    {
        if (string.IsNullOrWhiteSpace(username))
        {
            throw new SessionImportException("no Steam username given (set ORCH_STEAM_USERNAME)");
        }

        var path = Path.Combine(sessionDir, FileName);
        if (File.Exists(path))
        {
            SteamSession? saved;
            try
            {
                saved = JsonSerializer.Deserialize<SteamSession>(File.ReadAllText(path));
            }
            catch (JsonException)
            {
                throw new SessionImportException($"{path} is not a valid session file");
            }
            if (saved is null || string.IsNullOrEmpty(saved.RefreshToken))
            {
                throw new SessionImportException($"{path} holds no login token");
            }
            if (!string.Equals(saved.Username, username, StringComparison.OrdinalIgnoreCase))
            {
                throw new SessionImportException($"{path} holds a login for a different account");
            }
            return saved;
        }

        if (importFrom is null)
        {
            throw new SessionImportException($"no session at {path} and no --import-from directory");
        }
        var imported = ImportFromDepotDownloader(importFrom, username);
        Save(sessionDir, imported);
        return imported;
    }

    public static SteamSession ImportFromDepotDownloader(string configDir, string username)
    {
        var stores = Directory.Exists(configDir)
            ? Directory.GetFiles(configDir, "account.config", SearchOption.AllDirectories)
            : [];
        if (stores.Length == 0)
        {
            throw new SessionImportException($"no DepotDownloader account.config under {configDir}");
        }

        foreach (var store in stores)
        {
            DepotDownloaderAccountStore parsed;
            try
            {
                parsed = ReadStore(store);
            }
            catch (Exception e) when (e is InvalidDataException or ProtoException or IOException)
            {
                throw new SessionImportException($"cannot read {store}: {e.GetType().Name}");
            }
            foreach (var (account, token) in parsed.LoginTokens)
            {
                if (string.Equals(account, username, StringComparison.OrdinalIgnoreCase) && token.Length > 0)
                {
                    return new SteamSession(account, token);
                }
            }
        }
        throw new SessionImportException($"DepotDownloader holds no saved login for account '{username}'");
    }

    internal static DepotDownloaderAccountStore ReadStore(string path)
    {
        using var file = File.OpenRead(path);
        using var inflate = new DeflateStream(file, CompressionMode.Decompress);
        return Serializer.Deserialize<DepotDownloaderAccountStore>(inflate);
    }

    public static void Save(string sessionDir, SteamSession session)
    {
        // The token is private from the moment it touches disk: the directory is
        // created 0700 and the temp file is created 0600, never chmod-ed afterwards.
        if (OperatingSystem.IsWindows())
        {
            Directory.CreateDirectory(sessionDir);
        }
        else
        {
            Directory.CreateDirectory(sessionDir, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        }

        var path = Path.Combine(sessionDir, FileName);
        var temp = Path.Combine(sessionDir, $".{FileName}.{Path.GetRandomFileName()}");
        var options = new FileStreamOptions { Mode = FileMode.CreateNew, Access = FileAccess.Write };
        if (!OperatingSystem.IsWindows())
        {
            options.UnixCreateMode = UnixFileMode.UserRead | UnixFileMode.UserWrite;
        }

        try
        {
            using (var stream = new FileStream(temp, options))
            {
                JsonSerializer.Serialize(stream, session);
                stream.Flush(flushToDisk: true);
            }
            File.Move(temp, path, overwrite: true);
        }
        catch
        {
            File.Delete(temp);
            throw;
        }
    }
}
