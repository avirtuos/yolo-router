"yolo-router" is a python simulation of two load balancing system architectures. The first architecture is a basic round-robin system and the second is a more sophisticated least-conns style balancing technique. The simulation should allow you to choose which architecture you want to simulate as well as any configuration thats supported by that simulated architecture. At the end of the simulation run, we should generate a report on key metrics relevant to the simulation. Below we define the detailed requirements for each of the components and actors in this system.

Suggested libraries to build with:
- SimPy - discrete event simulation framework that also supports the concept of simulated time.
- SciPy - for stats and generating distributions of arrival rate date.
- NumPy - for stats and generating distributions of arrival rate date.

## Simulation Environment

All load balancing architecture that we will simulate will have:
- An incoming stream of requests that is generated using a configurable distribution function that varies the arrival rate, duration, and cpu demand of each request. A normal distribution function would be good to start with but we should plan to support tunable parameters for the p99, p90, p80, p50 of the duration and cpu demands in addition to the average arrival rate.
- A configurable number of "hosts" will be part of the load balancer so architectures that rely on only local host state may perform differently than architectures that intentionally coordinate across hosts. For example, an architecture may use a HashMap that is accessible to all load balancer hosts as a way to simulate a shared data store that allows for coordinated action.
- A configurable number of target hosts that the load balancing hosts send the requests to after making their load balancing decision.
- Access to a scaling service that offers 2 APIs. The first API is used to scale up the target fleet by adding a new target host. The second API is used to scale down the target fleet by removing a target host. These APIs take a "Scaling Delay" parameter that represents the amount of simulated time that it takes for the target host to be added or removed. This service isn't a real service, it too is a simulation.
- json config file that can be used to supply values for all the configurable parameters supported by the various features of this simulation

### Targets 

The load balancing architectures send traffic to "targets". A Target is essentially a server that is capable of servicing a request. Good examples are web servers or any http microservice. The features of a target may vary by architecture being tested. However, some features are universal. For example, we want to track: total requests, concurrent requests @ [p100, p99, p90, p80], request duration @ [p100, p99, p90, p80], request latency @ [p100, p99, p90, p80], CPU usage @ [p100, p99, p90, p80]. Request latency counts the end to end latency from the time the request was submitted to the load balancer to when it was completed on the Target. Request duration counts how long the request ran on the Target host.

### Load Balancing Architectures To Simulate:

This simulation binary should allow us to run simulations for any of the below load balancing approaches.

#### Round Robin

This is a simple load balancing architecture that maintains the same list of target hosts in the memory of all load balancing host. Each load balancing host runs a simple round robin to pick which target to send the next request to. If the target rejects the request (because it has exceeded its max concurrency) it can retry against the next host. The number of retries should be configurable. If the retries are exhausted and we've still not found a target that will accept the request, it should call the scaling service to trigger the creation of a new target and send the request there. If a target host has had fewer than a configurable number of requests in the last 5 minutes, a load balancer host will call the scaling service to scale down the idle target host. Whenever the scaling service is used to create or destroy a Target host, the load balancer host that trigger the action learns about the change as soon as the scaling service call completes but the all load balancer hosts learn about a change on a configurable delay (e.g. 100 simulated milliseconds). The report that is generated at the end of the simulation should include a histogram of how many requests required 1, 2, 3, etc retries. It should also include the p100, p99, p90, p80, p50 number of concurrent target hosts in the fleet. 


#### Least Conns

This is an advanced load balancing architecture that maintains a shared list of target hosts and count of outstanding requests per-target host across all load balancer hosts. When a new request arrives on a load balancer host it atomically picks the target host with the lowest number of outstanding requests and increments its outstanding requests count. These load balancer hosts are away of the max outstanding requests a target will accept. If all target hosts have exceeded their max connections, the load balancer host will call the scaling service to add an additional target host to the fleet. Once the scaler completes creating a new target host, the load balancer adds a new entry to the shared target map with an outstanding request count of 1 and sends the request to that host. If a target host has fewer than a configurable number of outstanding requests for 5 minutes or more, a load balancer host will call the scaler to terminate that target host and remove it from the shared list of target hosts. The report that is generated at the end of the simulation should include a histogram of how many requests required 1, 2, 3, etc retries. It should also include the p100, p99, p90, p80, p50 number of concurrent target hosts in the fleet. 

