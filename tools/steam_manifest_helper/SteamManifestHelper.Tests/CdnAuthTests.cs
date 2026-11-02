using System.Net;
using SteamManifestHelper;
using Xunit;

namespace SteamManifestHelper.Tests;

public sealed class CdnAuthTests
{
    private static HttpRequestException Forbidden() => new("forbidden", null, HttpStatusCode.Forbidden);

    [Fact]
    public async Task A_403_fetches_a_cdn_token_once_and_retries_with_it()
    {
        // Review Focus 3.
        var seen = new List<string?>();
        var fetches = 0;
        var result = await CdnAuth.WithAuthRetryAsync(
            token =>
            {
                seen.Add(token);
                return token is null ? Task.FromException<string>(Forbidden()) : Task.FromResult("manifest");
            },
            knownToken: null,
            fetchToken: () => { fetches++; return Task.FromResult<string?>("cdn-token"); });
        Assert.Equal("manifest", result);
        Assert.Equal(new string?[] { null, "cdn-token" }, seen);
        Assert.Equal(1, fetches);
    }

    [Fact]
    public async Task A_403_with_no_token_available_rethrows()
    {
        await Assert.ThrowsAsync<HttpRequestException>(() => CdnAuth.WithAuthRetryAsync(
            _ => Task.FromException<string>(Forbidden()),
            knownToken: null,
            fetchToken: () => Task.FromResult<string?>(null)));
    }

    [Fact]
    public async Task A_403_that_already_used_a_token_does_not_fetch_another()
    {
        var fetches = 0;
        await Assert.ThrowsAsync<HttpRequestException>(() => CdnAuth.WithAuthRetryAsync(
            _ => Task.FromException<string>(Forbidden()),
            knownToken: "cdn-token",
            fetchToken: () => { fetches++; return Task.FromResult<string?>("another"); }));
        Assert.Equal(0, fetches);
    }

    [Fact]
    public async Task Other_failures_are_not_retried()
    {
        var fetches = 0;
        await Assert.ThrowsAsync<HttpRequestException>(() => CdnAuth.WithAuthRetryAsync(
            _ => Task.FromException<string>(new HttpRequestException("gone", null, HttpStatusCode.NotFound)),
            knownToken: null,
            fetchToken: () => { fetches++; return Task.FromResult<string?>("cdn-token"); }));
        Assert.Equal(0, fetches);
    }
}
