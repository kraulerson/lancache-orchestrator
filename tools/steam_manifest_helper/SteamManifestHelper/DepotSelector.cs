using SteamKit2;

namespace SteamManifestHelper;

/// <summary>Where a depot's manifest id lives. RedirectAppId != 0 means the depot
/// borrows another app's files ("depotfromapp") and the id is in that app's info.</summary>
public readonly record struct ManifestLookup(ulong ManifestId, uint RedirectAppId);

/// <summary>DepotDownloader 3.4.0's depot rules for "-os windows -osarch 64", with
/// its defaults: English, no low-violence variants (ContentDownloader.cs:463-515,
/// GetSteam3DepotManifest at :206, GetDepotInfo at :558).</summary>
public static class DepotSelector
{
    public static IReadOnlyList<uint> SelectDepots(KeyValue depots, string os = "windows", string arch = "64", string language = "english")
    {
        var selected = new List<uint>();
        foreach (var depot in depots.Children)
        {
            if (depot.Children.Count == 0 || !uint.TryParse(depot.Name, out var depotId))
            {
                continue;
            }
            var config = depot["config"];
            if (config != KeyValue.Invalid &&
                (!Allows(config["oslist"], v => Array.IndexOf(v.Split(','), os) >= 0) ||
                 !Allows(config["osarch"], v => v == arch) ||
                 !Allows(config["language"], v => v == language) ||
                 (config["lowviolence"] != KeyValue.Invalid && config["lowviolence"].AsBoolean())))
            {
                continue;
            }
            selected.Add(depotId);
        }
        return selected;
    }

    // An absent or blank setting restricts nothing, as in DepotDownloader.
    private static bool Allows(KeyValue setting, Func<string, bool> accepts) =>
        setting == KeyValue.Invalid || string.IsNullOrWhiteSpace(setting.Value) || accepts(setting.Value);

    public static ManifestLookup ResolveManifest(KeyValue depots, uint depotId, uint appId, string branch = "public")
    {
        var depot = depots[depotId.ToString()];
        if (depot == KeyValue.Invalid)
        {
            return default;
        }
        if (depot["manifests"] == KeyValue.Invalid && depot["depotfromapp"] != KeyValue.Invalid)
        {
            var other = depot["depotfromapp"].AsUnsignedInteger();
            return other == appId ? default : new ManifestLookup(0, other);
        }
        return ulong.TryParse(depot["manifests"][branch]["gid"].Value, out var manifestId)
            ? new ManifestLookup(manifestId, 0)
            : default;
    }

    public static uint ContainingAppId(KeyValue appInfo, uint depotId, uint appId)
    {
        var proxy = appInfo["depots"][depotId.ToString()]["depotfromapp"];
        if (proxy == KeyValue.Invalid || appInfo["common"]["FreeToDownload"].AsBoolean())
        {
            return appId;
        }
        var proxyAppId = proxy.AsUnsignedInteger();
        return proxyAppId == 0 ? appId : proxyAppId;
    }
}
