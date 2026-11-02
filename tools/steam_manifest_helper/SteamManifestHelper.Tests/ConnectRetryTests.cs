using SteamManifestHelper;
using Xunit;

namespace SteamManifestHelper.Tests;

public sealed class ConnectRetryTests
{
    private readonly List<TimeSpan> delays = new();

    private Task Delay(TimeSpan wait)
    {
        delays.Add(wait);
        return Task.CompletedTask;
    }

    [Fact]
    public async Task A_connect_that_succeeds_on_the_second_attempt_waits_5_seconds_once()
    {
        var attempts = 0;
        await ConnectRetry.RunAsync(() =>
        {
            attempts++;
            return attempts == 1 ? throw new SessionLostException("Steam closed the connection") : Task.CompletedTask;
        }, Delay);
        Assert.Equal(2, attempts);
        Assert.Equal(new[] { TimeSpan.FromSeconds(5) }, delays);
    }

    [Fact]
    public async Task Three_failed_connects_throw_after_waits_of_5_and_15_seconds()
    {
        var attempts = 0;
        await Assert.ThrowsAsync<TimeoutException>(() => ConnectRetry.RunAsync(() =>
        {
            attempts++;
            return Task.FromException(new TimeoutException());
        }, Delay));
        Assert.Equal(3, attempts);
        Assert.Equal(new[] { TimeSpan.FromSeconds(5), TimeSpan.FromSeconds(15) }, delays);
    }

    [Fact]
    public async Task A_first_time_success_does_not_wait()
    {
        var attempts = 0;
        await ConnectRetry.RunAsync(() => { attempts++; return Task.CompletedTask; }, Delay);
        Assert.Equal(1, attempts);
        Assert.Empty(delays);
    }

    [Fact]
    public async Task A_failure_that_is_not_a_network_fault_is_not_retried()
    {
        var attempts = 0;
        await Assert.ThrowsAsync<OperationCanceledException>(() => ConnectRetry.RunAsync(() =>
        {
            attempts++;
            return Task.FromException(new OperationCanceledException());
        }, Delay));
        Assert.Equal(1, attempts);
        Assert.Empty(delays);
    }
}
