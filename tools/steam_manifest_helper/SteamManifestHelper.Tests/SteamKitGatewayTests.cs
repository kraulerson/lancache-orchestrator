using System.Net;
using SteamKit2;
using SteamManifestHelper;
using Xunit;

namespace SteamManifestHelper.Tests;

/// <summary>The gateway's pure helpers. The network half is proven only live (Task 10).</summary>
public sealed class SteamKitGatewayTests
{
    private const string LeakyUrl = "https://x/y?token=SECRET";

    [Fact]
    public void Describe_never_passes_a_library_exceptions_message_through()
    {
        foreach (var e in new Exception[]
                 {
                     new InvalidOperationException($"GET {LeakyUrl} failed"),
                     new HttpRequestException($"GET {LeakyUrl} failed"),
                     new HttpRequestException($"GET {LeakyUrl} failed", null, HttpStatusCode.Forbidden),
                     new SteamKitWebRequestException($"GET {LeakyUrl} failed"),
                 })
        {
            var text = SteamKitGateway.Describe(e);
            Assert.DoesNotContain("SECRET", text);
            Assert.DoesNotContain("?", text);
        }
    }

    [Fact]
    public void Describe_keeps_the_http_status_code()
    {
        Assert.Equal("HttpRequestException: HTTP 403",
            SteamKitGateway.Describe(new HttpRequestException("x", null, HttpStatusCode.Forbidden)));
    }

    [Fact]
    public void An_access_denied_depot_key_means_no_access()
    {
        Assert.Null(SteamKitGateway.MapDepotKey(11, EResult.AccessDenied, [1, 2, 3]));
    }

    [Fact]
    public void An_ok_depot_key_is_returned()
    {
        Assert.Equal(new byte[] { 1, 2, 3 }, SteamKitGateway.MapDepotKey(11, EResult.OK, [1, 2, 3]));
    }

    [Fact]
    public void Any_other_depot_key_result_is_a_request_failure_naming_it()
    {
        var e = Assert.Throws<SteamRequestException>(() => SteamKitGateway.MapDepotKey(11, EResult.Busy, [1, 2, 3]));
        Assert.Contains("Busy", e.Message);
        Assert.Contains("11", e.Message);
    }

    [Fact]
    public void StatusOf_reads_an_http_exceptions_status()
    {
        Assert.Equal(HttpStatusCode.NotFound,
            SteamKitGateway.StatusOf(new HttpRequestException("x", null, HttpStatusCode.NotFound)));
        Assert.Null(SteamKitGateway.StatusOf(new InvalidOperationException("x")));
    }

    [Fact]
    public void A_5xx_is_a_slow_failure_and_a_404_is_not()
    {
        Assert.True(SteamKitGateway.IsSlowFailure(new HttpRequestException("x", null, HttpStatusCode.ServiceUnavailable)));
        Assert.False(SteamKitGateway.IsSlowFailure(new HttpRequestException("x", null, HttpStatusCode.NotFound)));
    }
}
