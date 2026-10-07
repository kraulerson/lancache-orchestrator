using SteamKit2;
using SteamManifestHelper;
using Xunit;

namespace SteamManifestHelper.Tests;

public sealed class DepotSelectorTests
{
    private static KeyValue Depots(string body) => KeyValue.LoadFromString($"\"depots\"\n{{\n{body}\n}}")!;

    private static KeyValue App(string depotsBody, string common = "") =>
        KeyValue.LoadFromString($"\"appinfo\"\n{{\n\"common\"\n{{\n{common}\n}}\n\"depots\"\n{{\n{depotsBody}\n}}\n}}")!;

    [Fact]
    public void Keeps_windows_64_bit_english_depots_and_drops_the_rest()
    {
        var depots = Depots("""
            "731" { "config" { "oslist" "windows" "osarch" "64" } "manifests" { "public" { "gid" "1" } } }
            "732" { "config" { "oslist" "linux" } "manifests" { "public" { "gid" "2" } } }
            "733" { "config" { "oslist" "macos" } "manifests" { "public" { "gid" "3" } } }
            "734" { "config" { "oslist" "windows,macos" } "manifests" { "public" { "gid" "4" } } }
            "735" { "config" { "oslist" "windows" "osarch" "32" } "manifests" { "public" { "gid" "5" } } }
            "736" { "config" { "language" "german" } "manifests" { "public" { "gid" "6" } } }
            "737" { "config" { "lowviolence" "1" } "manifests" { "public" { "gid" "7" } } }
            "738" { "manifests" { "public" { "gid" "8" } } }
            "branches" { "public" { "buildid" "99" } }
            "baselanguages" "english"
            """);
        Assert.Equal(new uint[] { 731, 734, 738 }, DepotSelector.SelectDepots(depots));
    }

    [Fact]
    public void A_blank_setting_restricts_nothing()
    {
        var depots = Depots("""
            "731" { "config" { "oslist" "" "osarch" "" "language" "" } "manifests" { "public" { "gid" "1" } } }
            """);
        Assert.Equal(new uint[] { 731 }, DepotSelector.SelectDepots(depots));
    }

    [Fact]
    public void Resolves_the_public_manifest_id()
    {
        var depots = Depots("""
            "731" { "manifests" { "public" { "gid" "7617088375292372759" } } }
            """);
        Assert.Equal(new ManifestLookup(7617088375292372759, 0), DepotSelector.ResolveManifest(depots, 731, 730));
    }

    [Fact]
    public void A_depot_with_no_public_manifest_resolves_to_nothing()
    {
        var depots = Depots("""
            "731" { "manifests" { "beta" { "gid" "5" } } }
            "732" { "config" { "oslist" "windows" } }
            """);
        Assert.Equal(default, DepotSelector.ResolveManifest(depots, 731, 730));
        Assert.Equal(default, DepotSelector.ResolveManifest(depots, 732, 730));
        Assert.Equal(default, DepotSelector.ResolveManifest(depots, 999, 730));
    }

    [Fact]
    public void A_borrowed_depot_redirects_to_the_app_that_owns_it()
    {
        var depots = Depots("""
            "228988" { "depotfromapp" "228980" }
            """);
        Assert.Equal(new ManifestLookup(0, 228980), DepotSelector.ResolveManifest(depots, 228988, 730));
    }

    [Fact]
    public void A_depot_borrowing_from_itself_resolves_to_nothing()
    {
        var depots = Depots("""
            "731" { "depotfromapp" "730" }
            """);
        Assert.Equal(default, DepotSelector.ResolveManifest(depots, 731, 730));
    }

    [Fact]
    public void A_borrowed_depot_is_requested_through_its_owner_unless_the_app_is_free()
    {
        var paid = App("""
            "228988" { "depotfromapp" "228980" }
            """);
        var free = App("""
            "228988" { "depotfromapp" "228980" }
            """, common: "\"FreeToDownload\" \"1\"");
        Assert.Equal(228980u, DepotSelector.ContainingAppId(paid, 228988, 730));
        Assert.Equal(730u, DepotSelector.ContainingAppId(free, 228988, 730));
        Assert.Equal(730u, DepotSelector.ContainingAppId(paid, 731, 730));
    }
}
