using System.IO.Compression;
using ProtoBuf;
using SteamManifestHelper;
using Xunit;

namespace SteamManifestHelper.Tests;

// What DepotDownloader 3.4.0 really writes: fields 2, 4 and 5. Fields 2 and 5
// must be skipped by our reader, not trip it.
[ProtoContract]
internal sealed class FullDepotDownloaderStore
{
    [ProtoMember(2)] public Dictionary<string, int> ContentServerPenalty { get; set; } = new();
    [ProtoMember(4)] public Dictionary<string, string> LoginTokens { get; set; } = new();
    [ProtoMember(5)] public Dictionary<string, string> GuardData { get; set; } = new();
}

public sealed class SessionStoreTests : IDisposable
{
    private const string Token = "eyJ-test-refresh-token-0123456789";
    private readonly string root = Directory.CreateTempSubdirectory("smh-session-").FullName;

    public void Dispose() => Directory.Delete(root, recursive: true);

    private string WriteStore(Dictionary<string, string> tokens)
    {
        var configDir = Path.Combine(root, "dd");
        var path = Path.Combine(configDir, ".local", "share", "IsolatedStorage", "aa", "bb", "AssemFiles", "account.config");
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        using var file = File.Create(path);
        using var deflate = new DeflateStream(file, CompressionMode.Compress);
        Serializer.Serialize(deflate, new FullDepotDownloaderStore
        {
            ContentServerPenalty = { ["cache1.example"] = 3 },
            LoginTokens = tokens,
            GuardData = { ["kraulerson"] = "guard" },
        });
        return configDir;
    }

    [Fact]
    public void Imports_the_token_for_the_configured_account()
    {
        var dd = WriteStore(new() { ["kraulerson"] = Token });
        var session = SessionStore.ImportFromDepotDownloader(dd, "kraulerson");
        Assert.Equal("kraulerson", session.Username);
        Assert.Equal(Token, session.RefreshToken);
    }

    [Fact]
    public void Username_match_ignores_case()
    {
        var dd = WriteStore(new() { ["KRaulerson"] = Token });
        Assert.Equal(Token, SessionStore.ImportFromDepotDownloader(dd, "kraulerson").RefreshToken);
    }

    [Fact]
    public void Picks_the_configured_account_when_the_store_holds_two()
    {
        var dd = WriteStore(new() { ["someoneelse"] = "other-token", ["kraulerson"] = Token });
        Assert.Equal(Token, SessionStore.ImportFromDepotDownloader(dd, "kraulerson").RefreshToken);
    }

    [Fact]
    public void An_unknown_account_fails_without_revealing_any_token()
    {
        var dd = WriteStore(new() { ["someoneelse"] = Token });
        var e = Assert.Throws<SessionImportException>(() => SessionStore.ImportFromDepotDownloader(dd, "kraulerson"));
        Assert.Contains("kraulerson", e.Message);
        Assert.DoesNotContain(Token, e.Message);
    }

    [Fact]
    public void A_missing_store_fails_clearly()
    {
        var e = Assert.Throws<SessionImportException>(() => SessionStore.ImportFromDepotDownloader(Path.Combine(root, "nope"), "kraulerson"));
        Assert.Contains("account.config", e.Message);
    }

    [Fact]
    public void A_corrupt_store_fails_clearly()
    {
        var dd = WriteStore(new() { ["kraulerson"] = Token });
        var path = Directory.GetFiles(dd, "account.config", SearchOption.AllDirectories).Single();
        File.WriteAllBytes(path, [0xFF, 0xFE, 0x00, 0x13, 0x37]);
        var e = Assert.Throws<SessionImportException>(() => SessionStore.ImportFromDepotDownloader(dd, "kraulerson"));
        Assert.Contains("cannot read", e.Message);
    }

    [Fact]
    public void Load_imports_once_then_reuses_its_own_copy()
    {
        var dd = WriteStore(new() { ["kraulerson"] = Token });
        var sessionDir = Path.Combine(root, "helper");
        SessionStore.Load(sessionDir, "kraulerson", dd);
        Directory.Delete(dd, recursive: true);
        Assert.Equal(Token, SessionStore.Load(sessionDir, "kraulerson", dd).RefreshToken);
    }

    [Fact]
    public void The_saved_session_is_readable_only_by_its_owner()
    {
        if (OperatingSystem.IsWindows())
        {
            return;
        }
        var sessionDir = Path.Combine(root, "helper");
        SessionStore.Save(sessionDir, new SteamSession("kraulerson", Token));
        var mode = File.GetUnixFileMode(Path.Combine(sessionDir, SessionStore.FileName));
        Assert.Equal(UnixFileMode.UserRead | UnixFileMode.UserWrite, mode);
    }

    [Fact]
    public void The_session_directory_is_private_to_its_owner()
    {
        if (OperatingSystem.IsWindows())
        {
            return;
        }
        var sessionDir = Path.Combine(root, "helper");
        SessionStore.Save(sessionDir, new SteamSession("kraulerson", Token));
        Assert.Equal(
            UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute,
            File.GetUnixFileMode(sessionDir));
    }

    [Fact]
    public void Save_leaves_only_the_session_file_behind()
    {
        if (OperatingSystem.IsWindows())
        {
            return;
        }
        var sessionDir = Path.Combine(root, "helper");
        SessionStore.Save(sessionDir, new SteamSession("kraulerson", "first-token"));
        SessionStore.Save(sessionDir, new SteamSession("kraulerson", Token));
        var files = Directory.GetFileSystemEntries(sessionDir);
        Assert.Equal([Path.Combine(sessionDir, SessionStore.FileName)], files);
        Assert.Equal(Token, SessionStore.Load(sessionDir, "kraulerson", null).RefreshToken);
    }

    [Fact]
    public void A_saved_session_for_another_account_is_refused()
    {
        var sessionDir = Path.Combine(root, "helper");
        SessionStore.Save(sessionDir, new SteamSession("someoneelse", Token));
        Assert.Throws<SessionImportException>(() => SessionStore.Load(sessionDir, "kraulerson", null));
    }

    [Fact]
    public void An_empty_username_names_the_setting_to_fix()
    {
        var e = Assert.Throws<SessionImportException>(() => SessionStore.Load(Path.Combine(root, "helper"), "", null));
        Assert.Contains("ORCH_STEAM_USERNAME", e.Message);
    }

    [Fact]
    public void A_session_never_prints_its_token()
    {
        Assert.DoesNotContain(Token, new SteamSession("kraulerson", Token).ToString());
    }
}
