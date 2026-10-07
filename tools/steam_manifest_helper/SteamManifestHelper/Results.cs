using System.Text.Json;
using System.Text.Json.Serialization;

namespace SteamManifestHelper;

public static class ExitCodes
{
    public const int Completed = 0;
    public const int Unexpected = 1;
    public const int LoginRefused = 2;
    public const int ImportFailed = 3;
    public const int Disconnected = 4;
    public const int Usage = 64;
}

public static class AppStatus
{
    public const string Ok = "ok";
    public const string NoDepots = "no_depots";
    public const string NotOwned = "not_owned";
    public const string Error = "error";
    public const string NotAttempted = "not_attempted";
}

public static class SessionStatus
{
    public const string Completed = "completed";
    public const string LoginRefused = "login_refused";
    public const string ImportFailed = "import_failed";
    public const string Disconnected = "disconnected";
}

public sealed record AppResult(uint App, string Status, IReadOnlyList<string>? Manifests = null, string? Reason = null);

public sealed record RunSummary(string Session, string Reason)
{
    public bool Summary => true;
}

/// <summary>stdout is a JSON-lines channel for Python. Nothing else may write to it.</summary>
public static class ResultWriter
{
    private static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    public static void Write(TextWriter output, AppResult line) => Emit(output, JsonSerializer.Serialize(line, Options));

    public static void Write(TextWriter output, RunSummary line) => Emit(output, JsonSerializer.Serialize(line, Options));

    private static void Emit(TextWriter output, string json)
    {
        output.WriteLine(json);
        output.Flush();
    }
}
