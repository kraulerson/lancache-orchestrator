using SteamManifestHelper;
using Xunit;

namespace SteamManifestHelper.Tests;

public sealed class RequestRetryTests
{
    private readonly List<(int Attempt, Exception Error)> retries = new();

    private void OnRetry(int attempt, Exception error) => retries.Add((attempt, error));

    [Fact]
    public async Task A_first_time_success_makes_one_call_and_does_not_report_a_retry()
    {
        var calls = 0;
        var result = await RequestRetry.RunAsync(() => { calls++; return Task.FromResult(42); }, 3, OnRetry);
        Assert.Equal(42, result);
        Assert.Equal(1, calls);
        Assert.Empty(retries);
    }

    [Fact]
    public async Task One_dropped_request_is_re_sent_and_reported_as_attempt_1()
    {
        var calls = 0;
        var result = await RequestRetry.RunAsync(() =>
        {
            calls++;
            return calls == 1 ? throw new SteamRequestException("depot key 1: timed out") : Task.FromResult(7);
        }, 3, OnRetry);
        Assert.Equal(7, result);
        Assert.Equal(2, calls);
        var retry = Assert.Single(retries);
        Assert.Equal(1, retry.Attempt);
        Assert.IsType<SteamRequestException>(retry.Error);
    }

    [Fact]
    public async Task Three_dropped_requests_rethrow_the_last_SteamRequestException()
    {
        var calls = 0;
        var thrown = await Assert.ThrowsAsync<SteamRequestException>(() => RequestRetry.RunAsync<int>(() =>
        {
            calls++;
            return Task.FromException<int>(new SteamRequestException($"no answer {calls}"));
        }, 3, OnRetry));
        Assert.Equal(3, calls);
        Assert.Equal("no answer 3", thrown.Message);
        Assert.Equal(2, retries.Count);
    }

    [Fact]
    public async Task A_lost_session_propagates_at_once_without_a_retry()
    {
        var calls = 0;
        await Assert.ThrowsAsync<SessionLostException>(() => RequestRetry.RunAsync<int>(() =>
        {
            calls++;
            return Task.FromException<int>(new SessionLostException("Steam closed the connection"));
        }, 3, OnRetry));
        Assert.Equal(1, calls);
        Assert.Empty(retries);
    }

    [Fact]
    public async Task Any_other_exception_propagates_at_once_without_a_retry()
    {
        var calls = 0;
        await Assert.ThrowsAsync<InvalidOperationException>(() => RequestRetry.RunAsync<int>(() =>
        {
            calls++;
            return Task.FromException<int>(new InvalidOperationException());
        }, 3, OnRetry));
        Assert.Equal(1, calls);
        Assert.Empty(retries);
    }
}
