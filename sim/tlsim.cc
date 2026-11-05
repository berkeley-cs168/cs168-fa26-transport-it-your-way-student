// The ns-3 side of a trial, as a Python extension module: transportlab._tlsim.
//
// transportlab/simrun.py builds the network with configure(), registers what
// each host should send and receive, then calls run(). During run() ns-3 calls
// back into Python through one dispatcher function, and student code calls in
// here (now, send, set_timer, deliver) through transportlab/api.py.
//
// Everything that gets graded is counted here, not in Python: datagrams and
// wire bytes at the socket, deliveries checked against the expected payload.
// run() writes those counts to sim-stats.json when the trial ends.
//
// Determinism: simulated time is integer nanoseconds, link rates are integer
// bits per second, every random number comes from a seeded ns-3 stream with a
// fixed stream number, and nothing here iterates an unordered container.

#include "ns3/core-config.h"
#include "ns3/core-module.h"
#include "ns3/internet-module.h"
#include "ns3/network-module.h"
#include "ns3/point-to-point-module.h"
#include "ns3/queue-disc.h"
#include "ns3/traffic-control-layer.h"

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdint>
#include <deque>
#include <fstream>
#include <map>
#include <set>
#include <string>
#include <vector>

#ifndef INT64X64_USE_128
#error "ns-3 must be configured with NS3_INT64X64=INT128 so x86 and ARM agree"
#endif

namespace py = pybind11;
using namespace ns3;

namespace
{

constexpr uint32_t kWireHeaderBytes = 28; // IPv4 + UDP, as transportlab.WIRE_HEADER_BYTES
constexpr uint32_t kMaxDatagram = 1472;   // as transportlab.api.MAX_DATAGRAM

// One direction of a link, in the units ns-3 wants.
struct DirParams
{
    uint64_t bps;
    int64_t delayNs;
    int64_t jitterNs;
    double loss; // probability, 0 to 1
    uint32_t queuePkts;
};

DirParams
ParseDir(const py::dict& d)
{
    DirParams p;
    p.bps = d["bps"].cast<uint64_t>();
    p.delayNs = d["delay_ns"].cast<int64_t>();
    p.jitterNs = d["jitter_ns"].cast<int64_t>();
    p.loss = d["loss"].cast<double>();
    p.queuePkts = d["queue_pkts"].cast<uint32_t>();
    return p;
}

} // namespace

namespace ns3
{

// A drop-tail queue that also drops a fraction of packets at enqueue, the way
// netem's loss does. The loss draw happens for every packet, even at zero
// loss, so turning loss on mid-run does not shift every later random number.
class TlQueue : public DropTailQueue<Packet>
{
  public:
    static TypeId GetTypeId()
    {
        static TypeId tid = TypeId("ns3::TlQueue")
                                .SetParent<DropTailQueue<Packet>>()
                                .SetGroupName("Network")
                                .AddConstructor<TlQueue>();
        return tid;
    }

    void SetLoss(double loss) { m_loss = loss; }

    void SetRandom(Ptr<UniformRandomVariable> rv) { m_rv = rv; }

    bool Enqueue(Ptr<Packet> item) override
    {
        double u = m_rv->GetValue();
        if (u < m_loss)
        {
            lossDrops++;
            DropBeforeEnqueue(item);
            return false;
        }
        return DropTailQueue<Packet>::Enqueue(item);
    }

    // Drop everything waiting, as at the start of a blackout. Returns how many.
    uint32_t DropAll()
    {
        uint32_t n = 0;
        while (!IsEmpty())
        {
            Remove();
            n++;
        }
        flushDrops += n;
        return n;
    }

    uint64_t lossDrops = 0;
    uint64_t flushDrops = 0;

  private:
    double m_loss = 0.0;
    Ptr<UniformRandomVariable> m_rv;
};

NS_OBJECT_ENSURE_REGISTERED(TlQueue);

// A point-to-point channel with a delay and jitter per direction, which also
// remembers the packets it has in flight so a blackout can drop them.
class TlChannel : public PointToPointChannel
{
  public:
    static TypeId GetTypeId()
    {
        static TypeId tid = TypeId("ns3::TlChannel")
                                .SetParent<PointToPointChannel>()
                                .SetGroupName("PointToPoint")
                                .AddConstructor<TlChannel>();
        return tid;
    }

    void SetDirection(uint32_t wire, int64_t delayNs, int64_t jitterNs)
    {
        m_delayNs[wire] = delayNs;
        m_jitterNs[wire] = jitterNs;
    }

    void SetJitterRandom(uint32_t wire, Ptr<UniformRandomVariable> rv) { m_jitterRv[wire] = rv; }

    // Which direction a device sends on: 0 for the link's `a` side.
    uint32_t WireOf(Ptr<PointToPointNetDevice> src) const { return src == GetSource(0) ? 0 : 1; }

    bool TransmitStart(Ptr<const Packet> p, Ptr<PointToPointNetDevice> src, Time txTime) override
    {
        uint32_t wire = WireOf(src);
        int64_t delay = m_delayNs[wire];
        if (m_jitterNs[wire] > 0)
        {
            // Uniform in [-jitter, +jitter]. Integer nanoseconds, no libm.
            int64_t span = 2 * m_jitterNs[wire] + 1;
            int64_t offset = static_cast<int64_t>(m_jitterRv[wire]->GetInteger(0, span - 1));
            delay = std::max<int64_t>(0, delay + offset - m_jitterNs[wire]);
        }
        Ptr<PointToPointNetDevice> dst = GetDestination(wire);
        // Plain Schedule rather than ScheduleWithContext, because only Schedule
        // returns an EventId we can cancel. The context is only used by
        // context-based trace paths, which nothing here uses.
        EventId ev = Simulator::Schedule(txTime + NanoSeconds(delay),
                                         &PointToPointNetDevice::Receive,
                                         dst,
                                         p->Copy());
        auto& flight = m_inFlight[wire];
        while (!flight.empty() && flight.front().IsExpired())
        {
            flight.pop_front();
        }
        flight.push_back(ev);
        if (onTransmit)
        {
            onTransmit(wire, p, txTime, txTime + NanoSeconds(delay));
        }
        return true;
    }

    // Drop every packet on the wire in one direction. Returns how many.
    uint32_t DropInFlight(uint32_t wire)
    {
        uint32_t n = 0;
        for (auto& ev : m_inFlight[wire])
        {
            if (!ev.IsExpired())
            {
                ev.Cancel();
                n++;
            }
        }
        m_inFlight[wire].clear();
        return n;
    }

    // Set by the trial for the viewer's event log.
    std::function<void(uint32_t, Ptr<const Packet>, Time, Time)> onTransmit;

  private:
    int64_t m_delayNs[2] = {0, 0};
    int64_t m_jitterNs[2] = {0, 0};
    Ptr<UniformRandomVariable> m_jitterRv[2];
    std::deque<EventId> m_inFlight[2];
};

NS_OBJECT_ENSURE_REGISTERED(TlChannel);

} // namespace ns3

namespace
{

struct Expected
{
    uint32_t size;
    int64_t offeredNs;
    int64_t deadlineNs; // -1 when the message has no deadline
    std::string payload;
};

struct Sample
{
    int64_t msgId;
    int64_t latencyNs;
    int64_t tNs;
};

// Counters for one host. Names match transportlab.api._Counters.
struct HostStats
{
    uint64_t datagramsSent = 0, datagramsRecv = 0;
    uint64_t wireBytesSent = 0, wireBytesRecv = 0;
    uint64_t appBytesOffered = 0, appBytesDelivered = 0;
    uint64_t offered = 0, delivered = 0;
    uint64_t duplicates = 0, corrupt = 0, unknown = 0, late = 0;
    std::map<int64_t, uint64_t> wireBuckets, appBuckets;
    std::vector<Sample> samples;
};

struct Host
{
    std::string name;
    std::string role; // "sender", "receiver", or "" for a host with no traffic
    Ipv4Address ip;
    uint16_t port;
    Ptr<Node> node;
    Ptr<Socket> socket;
    bool alive = true;
    bool crashed = false;
    std::string error;
    std::map<std::string, EventId> timers;
    std::map<int64_t, Expected> expected;
    std::set<int64_t> done;
    // Sender only: (offered_ns, msg_id, size), in offer order.
    std::vector<std::tuple<int64_t, int64_t, uint32_t>> schedule;
    size_t nextOffer = 0;
    HostStats stats;
};

struct Link
{
    std::string a, b;
    Ptr<TlChannel> channel;
    Ptr<PointToPointNetDevice> dev[2]; // dev[0] on a, sends on wire 0
    Ptr<TlQueue> queue[2];
    uint64_t flightDrops[2] = {0, 0};
    bool dark[2] = {false, false}; // dropping everything, as in a blackout
};

struct Trial
{
    bool ran = false;
    uint64_t seed = 0;
    int64_t durationNs = 0;
    int64_t bucketNs = 250000000;
    std::vector<Host> hosts;
    std::map<std::string, size_t> hostByName;
    std::map<uint32_t, size_t> hostByIp; // Ipv4Address::Get() -> host index
    std::vector<Ptr<Node>> switches;
    std::vector<Link> links;
    py::object dispatcher;
    std::string label;
    Host* current = nullptr; // the host whose callback is running
    std::ofstream vis;
};

Trial* g_trial = nullptr;

Trial&
T()
{
    if (g_trial == nullptr)
    {
        throw std::runtime_error("transportlab._tlsim: configure() has not been called");
    }
    return *g_trial;
}

Host&
Current()
{
    Host* h = T().current;
    if (h == nullptr)
    {
        throw std::runtime_error(
            "the simulator API can only be used from inside a protocol callback");
    }
    return *h;
}

int64_t
NowNs()
{
    return Simulator::Now().GetNanoSeconds();
}

// A fixed, seeded order for events that land at the same instant, so four
// incast senders offered at t=1.0 don't always go in host order.
uint64_t
Mix(uint64_t x)
{
    x += 0x9e3779b97f4a7c15ULL;
    x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
    x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
    return x ^ (x >> 31);
}

void
VisLine(const std::string& line)
{
    Trial& t = T();
    if (t.vis.is_open())
    {
        t.vis << NowNs() << ' ' << line << '\n';
    }
}

void
VisQueue(std::string key, uint32_t, uint32_t now)
{
    VisLine("queue " + key + " " + std::to_string(now));
}

void
VisDrop(std::string key, Ptr<const Packet> p)
{
    VisLine("drop " + key + " " + std::to_string(p->GetSize()));
}

// Call Python for one host. A Python error that escapes the protocol's own
// guard (SystemExit, KeyboardInterrupt, a bug in the harness) marks that host
// crashed; the rest of the trial carries on.
void
Dispatch(Host& h, const char* kind, py::object a = py::none(), py::object b = py::none())
{
    // A host that stopped or raised in its own code still gets on_stop, as it
    // does under run-local. One whose error escaped the guard gets nothing more.
    bool stopping = std::string(kind) == "stop";
    if (!h.alive && !(stopping && !h.crashed))
    {
        return;
    }
    Trial& t = T();
    Host* saved = t.current;
    t.current = &h;
    try
    {
        py::object alive = t.dispatcher(h.name, kind, a, b);
        if (!alive.cast<bool>())
        {
            h.alive = false;
        }
    }
    catch (py::error_already_set& e)
    {
        h.alive = false;
        h.crashed = true;
        h.error = e.what();
    }
    t.current = saved;
}

void
OnReceive(size_t hostIndex, Ptr<Socket> socket)
{
    Host& h = T().hosts[hostIndex];
    Address from;
    while (Ptr<Packet> p = socket->RecvFrom(from))
    {
        uint32_t n = p->GetSize();
        h.stats.datagramsRecv++;
        h.stats.wireBytesRecv += n + kWireHeaderBytes;
        if (!h.alive)
        {
            continue;
        }
        std::string data(n, '\0');
        p->CopyData(reinterpret_cast<uint8_t*>(data.data()), n);
        InetSocketAddress addr = InetSocketAddress::ConvertFrom(from);
        std::ostringstream ip;
        addr.GetIpv4().Print(ip);
        Dispatch(h,
                 "datagram",
                 py::bytes(data),
                 py::make_tuple(ip.str(), static_cast<int>(addr.GetPort())));
    }
}

void
OnTimer(size_t hostIndex, std::string name)
{
    Host& h = T().hosts[hostIndex];
    h.timers.erase(name);
    Dispatch(h, "timer", py::str(name));
}

void
OnOffer(size_t hostIndex)
{
    Host& h = T().hosts[hostIndex];
    if (!h.alive)
    {
        return;
    }
    int64_t now = NowNs();
    while (h.nextOffer < h.schedule.size() && std::get<0>(h.schedule[h.nextOffer]) <= now)
    {
        h.stats.offered++;
        h.stats.appBytesOffered += std::get<2>(h.schedule[h.nextOffer]);
        h.nextOffer++;
    }
    Dispatch(h, "offer");
}

void
OnStart(size_t hostIndex)
{
    Dispatch(T().hosts[hostIndex], "start");
}

void
ApplyDir(Link& link, uint32_t wire, const DirParams& p)
{
    link.dark[wire] = p.loss >= 1.0;
    link.dev[wire]->SetDataRate(DataRate(p.bps));
    link.queue[wire]->SetLoss(p.loss);
    link.queue[wire]->SetMaxSize(QueueSize(QueueSizeUnit::PACKETS, p.queuePkts));
    link.channel->SetDirection(wire, p.delayNs, p.jitterNs);
}

void Route(bool upOnly);

// A link event from the workload card, or a satellite pass. `flush` is set at the start of a
// blackout: the link drops what is queued and what is already on the wire.
void
OnLinkEvent(size_t linkIndex, uint32_t wire, DirParams p, bool flush, std::string label)
{
    Link& link = T().links[linkIndex];
    bool wasDark = link.dark[wire];
    ApplyDir(link, wire, p);
    if (link.dark[wire] != wasDark)
    {
        Route(true);
    }
    uint32_t queued = 0, flying = 0;
    if (flush)
    {
        queued = link.queue[wire]->DropAll();
        flying = link.channel->DropInFlight(wire);
        link.flightDrops[wire] += flying;
    }
    std::string from = wire == 0 ? link.a : link.b;
    std::string to = wire == 0 ? link.b : link.a;
    VisLine("event " + from + " " + to + " " + label + " " + std::to_string(p.bps) + " " +
            std::to_string(queued) + " " + std::to_string(flying));
}

// Routes. Each switch gets a host route to every host, out of the interface
// on the shortest path, found by a breadth-first search over the links in
// manifest order, so ties go to the link listed first.
//
// With `upOnly`, links that are dark in either direction don't count, and a
// switch with no path left keeps the route it had. That is how traffic
// follows a satellite pass: when one satellite goes out of view and the next
// comes up, the routes move to it. Called once at configure with every link,
// then whenever a link goes dark or comes back.
void
Route(bool upOnly)
{
    Trial& t = T();
    std::map<Ptr<Node>, std::vector<std::pair<Ptr<Node>, uint32_t>>> adj; // node -> (peer, ifIndex)
    for (auto& link : t.links)
    {
        if (upOnly && (link.dark[0] || link.dark[1]))
        {
            continue;
        }
        Ptr<Node> n0 = link.dev[0]->GetNode(), n1 = link.dev[1]->GetNode();
        uint32_t if0 = n0->GetObject<Ipv4>()->GetInterfaceForDevice(link.dev[0]);
        uint32_t if1 = n1->GetObject<Ipv4>()->GetInterfaceForDevice(link.dev[1]);
        adj[n0].push_back({n1, if0});
        adj[n1].push_back({n0, if1});
    }
    Ipv4StaticRoutingHelper routing;
    for (auto& sw : t.switches)
    {
        Ptr<Ipv4StaticRouting> table = routing.GetStaticRouting(sw->GetObject<Ipv4>());
        for (auto& h : t.hosts)
        {
            // Search from the host outward; the first time we reach the switch,
            // the hop we came from is the way back toward the host.
            std::map<Ptr<Node>, Ptr<Node>> parent;
            std::deque<Ptr<Node>> frontier{h.node};
            parent[h.node] = nullptr;
            while (!frontier.empty() && parent.find(sw) == parent.end())
            {
                Ptr<Node> n = frontier.front();
                frontier.pop_front();
                for (auto& [peer, _] : adj[n])
                {
                    if (parent.find(peer) == parent.end())
                    {
                        parent[peer] = n;
                        frontier.push_back(peer);
                    }
                }
            }
            if (parent.find(sw) == parent.end())
            {
                continue;
            }
            Ptr<Node> next = parent[sw];
            for (auto& [peer, ifIndex] : adj[sw])
            {
                if (peer == next)
                {
                    for (uint32_t i = table->GetNRoutes(); i-- > 0;)
                    {
                        Ipv4RoutingTableEntry e = table->GetRoute(i);
                        if (e.IsHost() && e.GetDest() == h.ip)
                        {
                            table->RemoveRoute(i);
                        }
                    }
                    table->AddHostRouteTo(h.ip, ifIndex);
                    break;
                }
            }
        }
    }
}

// ---- configure -------------------------------------------------------------

void
Configure(const py::dict& spec)
{
    if (g_trial != nullptr)
    {
        throw std::runtime_error("configure() can only be called once per process");
    }
    g_trial = new Trial();
    Trial& t = *g_trial;

    t.seed = spec["seed"].cast<uint64_t>();
    t.durationNs = spec["duration_ns"].cast<int64_t>();
    t.bucketNs = spec["bucket_ns"].cast<int64_t>();

    // MRG32k3a rejects seed 0, so the trial seed goes in as the run number.
    RngSeedManager::SetSeed(1);
    RngSeedManager::SetRun(t.seed + 1);
    // Real checksums, so Wireshark does not flag every header.
    GlobalValue::Bind("ChecksumEnabled", BooleanValue(true));

    std::string visPath = spec["vis_path"].cast<std::string>();
    if (!visPath.empty())
    {
        t.vis.open(visPath);
    }

    InternetStackHelper stack;
    stack.SetIpv6StackInstall(false);

    // Hosts, in scenario order. Their index sets the address and port.
    for (auto item : spec["hosts"].cast<py::list>())
    {
        py::dict d = item.cast<py::dict>();
        Host h;
        h.name = d["name"].cast<std::string>();
        h.role = d["role"].cast<std::string>();
        h.ip = Ipv4Address(d["ip"].cast<std::string>().c_str());
        h.port = d["port"].cast<uint16_t>();
        h.node = CreateObject<Node>();
        stack.Install(h.node);
        t.hostByName[h.name] = t.hosts.size();
        t.hostByIp[h.ip.Get()] = t.hosts.size();
        t.hosts.push_back(std::move(h));
    }
    std::map<std::string, Ptr<Node>> switchByName;
    for (auto item : spec["switches"].cast<py::list>())
    {
        Ptr<Node> n = CreateObject<Node>();
        stack.Install(n);
        switchByName[item.cast<std::string>()] = n;
        t.switches.push_back(n);
    }
    auto nodeOf = [&](const std::string& name) -> Ptr<Node> {
        auto hit = t.hostByName.find(name);
        if (hit != t.hostByName.end())
        {
            return t.hosts[hit->second].node;
        }
        auto sw = switchByName.find(name);
        if (sw == switchByName.end())
        {
            throw std::runtime_error("link names an unknown node: " + name);
        }
        return sw->second;
    };

    // Links, in manifest order, so MAC addresses and stream numbers are stable.
    PcapHelper pcap;
    std::string pcapDir = spec["pcap_dir"].cast<std::string>();
    uint32_t snaplen = spec["pcap_snaplen"].cast<uint32_t>();
    std::map<std::string, int> hostLinks;
    uint32_t linkIndex = 0;
    for (auto item : spec["links"].cast<py::list>())
    {
        py::dict d = item.cast<py::dict>();
        Link link;
        link.a = d["a"].cast<std::string>();
        link.b = d["b"].cast<std::string>();
        link.channel = CreateObject<TlChannel>();
        Ptr<Node> ends[2] = {nodeOf(link.a), nodeOf(link.b)};
        DirParams dirs[2] = {ParseDir(d["a_to_b"].cast<py::dict>()),
                             ParseDir(d["b_to_a"].cast<py::dict>())};
        for (uint32_t w = 0; w < 2; w++)
        {
            Ptr<PointToPointNetDevice> dev = CreateObject<PointToPointNetDevice>();
            dev->SetAddress(Mac48Address::Allocate());
            Ptr<TlQueue> q = CreateObject<TlQueue>();
            Ptr<UniformRandomVariable> lossRv = CreateObject<UniformRandomVariable>();
            lossRv->SetStream(4 * linkIndex + 2 * w);
            q->SetRandom(lossRv);
            dev->SetQueue(q);
            ends[w]->AddDevice(dev);
            dev->Attach(link.channel);
            Ptr<UniformRandomVariable> jitterRv = CreateObject<UniformRandomVariable>();
            jitterRv->SetStream(4 * linkIndex + 2 * w + 1);
            link.channel->SetJitterRandom(w, jitterRv);
            link.dev[w] = dev;
            link.queue[w] = q;
        }
        for (uint32_t w = 0; w < 2; w++)
        {
            ApplyDir(link, w, dirs[w]);
        }

        // Addresses. A host's one device gets 10.0.0.N/24. A switch's devices
        // get 10.255.<link>.1 or .2, which only exist so the interface is up.
        for (uint32_t w = 0; w < 2; w++)
        {
            const std::string& name = w == 0 ? link.a : link.b;
            Ptr<Ipv4> ipv4 = ends[w]->GetObject<Ipv4>();
            int32_t ifIndex = ipv4->AddInterface(link.dev[w]);
            auto hit = t.hostByName.find(name);
            Ipv4InterfaceAddress addr;
            if (hit != t.hostByName.end())
            {
                hostLinks[name]++;
                addr = Ipv4InterfaceAddress(t.hosts[hit->second].ip, Ipv4Mask("255.255.255.0"));
            }
            else
            {
                std::ostringstream s;
                s << "10.255." << linkIndex << "." << (w + 1);
                addr = Ipv4InterfaceAddress(Ipv4Address(s.str().c_str()),
                                            Ipv4Mask("255.255.255.0"));
            }
            ipv4->AddAddress(ifIndex, addr);
            ipv4->SetUp(ifIndex);

            if (!pcapDir.empty())
            {
                std::string other = w == 0 ? link.b : link.a;
                std::string file = hit != t.hostByName.end() ? name + ".pcap"
                                                             : name + "-" + other + ".pcap";
                Ptr<PcapFileWrapper> f =
                    pcap.CreateFile(pcapDir + "/" + file, std::ios::out, PcapHelper::DLT_PPP,
                                    snaplen);
                pcap.HookDefaultSink<PointToPointNetDevice>(link.dev[w], "PromiscSniffer", f);
            }
        }

        if (t.vis.is_open())
        {
            std::string a = link.a, b = link.b;
            link.channel->onTransmit = [a, b](uint32_t wire, Ptr<const Packet> p, Time tx,
                                              Time arrive) {
                VisLine("tx " + (wire == 0 ? a : b) + " " + (wire == 0 ? b : a) + " " +
                        std::to_string(p->GetSize()) + " " +
                        std::to_string(tx.GetNanoSeconds()) + " " +
                        std::to_string(arrive.GetNanoSeconds()));
            };
            for (uint32_t w = 0; w < 2; w++)
            {
                std::string from = w == 0 ? a : b, to = w == 0 ? b : a;
                link.queue[w]->TraceConnectWithoutContext(
                    "PacketsInQueue",
                    MakeBoundCallback(&VisQueue, from + " " + to));
                link.queue[w]->TraceConnectWithoutContext(
                    "Drop",
                    MakeBoundCallback(&VisDrop, from + " " + to));
            }
        }

        t.links.push_back(link);
        linkIndex++;
    }

    for (auto& h : t.hosts)
    {
        if (hostLinks[h.name] != 1)
        {
            throw std::runtime_error("host " + h.name + " must have exactly one link, has " +
                                     std::to_string(hostLinks[h.name]));
        }
    }

    Route(false);

    // No queue disc anywhere: the device's drop-tail queue is the only queue.
    std::vector<Ptr<Node>> all;
    for (auto& h : t.hosts)
    {
        all.push_back(h.node);
    }
    all.insert(all.end(), t.switches.begin(), t.switches.end());
    for (auto& n : all)
    {
        Ptr<TrafficControlLayer> tc = n->GetObject<TrafficControlLayer>();
        for (uint32_t i = 0; i < n->GetNDevices(); i++)
        {
            Ptr<NetDevice> dev = n->GetDevice(i);
            if (tc && tc->GetRootQueueDiscOnDevice(dev))
            {
                tc->DeleteRootQueueDiscOnDevice(dev);
            }
        }
    }

    // One UDP socket per host, bound to its address and port.
    for (size_t i = 0; i < t.hosts.size(); i++)
    {
        Host& h = t.hosts[i];
        h.socket = Socket::CreateSocket(h.node, UdpSocketFactory::GetTypeId());
        h.socket->SetAttribute("RcvBufSize", UintegerValue(64u << 20));
        if (h.socket->Bind(InetSocketAddress(h.ip, h.port)) != 0)
        {
            throw std::runtime_error("cannot bind socket for " + h.name);
        }
        h.socket->SetRecvCallback(MakeBoundCallback(&OnReceive, i));
    }

    // Link events, already merged onto the baseline by the orchestrator.
    for (auto item : spec["events"].cast<py::list>())
    {
        py::dict d = item.cast<py::dict>();
        Simulator::Schedule(NanoSeconds(d["t_ns"].cast<int64_t>()),
                            &OnLinkEvent,
                            d["link"].cast<size_t>(),
                            d["wire"].cast<uint32_t>(),
                            ParseDir(d["params"].cast<py::dict>()),
                            d["flush"].cast<bool>(),
                            d["label"].cast<std::string>());
    }
}

// ---- registration, before run() -------------------------------------------

Host&
HostNamed(const std::string& name)
{
    Trial& t = T();
    auto hit = t.hostByName.find(name);
    if (hit == t.hostByName.end())
    {
        throw std::runtime_error("no host named " + name);
    }
    return t.hosts[hit->second];
}

void
Expect(const std::string& host, int64_t msgId, uint32_t size, int64_t offeredNs,
       int64_t deadlineNs, py::bytes payload)
{
    if (T().ran)
    {
        throw std::runtime_error("expect() after run()");
    }
    HostNamed(host).expected[msgId] = Expected{size, offeredNs, deadlineNs, std::string(payload)};
}

void
Offer(const std::string& host, int64_t msgId, uint32_t size, int64_t offeredNs)
{
    if (T().ran)
    {
        throw std::runtime_error("offer() after run()");
    }
    HostNamed(host).schedule.emplace_back(offeredNs, msgId, size);
}

void WriteStats(const std::string& path);

void
Run(py::object dispatcher, const std::string& statsPath)
{
    Trial& t = T();
    if (t.ran)
    {
        throw std::runtime_error("run() can only be called once");
    }
    t.ran = true;
    t.dispatcher = dispatcher;

    // Starts, receivers first so nothing arrives before a receiver is ready,
    // then by name. All at time zero, before anything else at time zero.
    std::vector<size_t> order(t.hosts.size());
    for (size_t i = 0; i < order.size(); i++)
    {
        order[i] = i;
    }
    std::sort(order.begin(), order.end(), [&](size_t x, size_t y) {
        const Host& a = t.hosts[x];
        const Host& b = t.hosts[y];
        return std::make_pair(a.role != "receiver", a.name) <
               std::make_pair(b.role != "receiver", b.name);
    });
    for (size_t i : order)
    {
        if (!t.hosts[i].role.empty())
        {
            Simulator::ScheduleNow(&OnStart, i);
        }
    }

    // Offers: one event per sender per distinct time. Events at the same
    // instant go in a seeded order rather than host order.
    std::vector<std::tuple<int64_t, uint64_t, size_t>> offers;
    for (size_t i = 0; i < t.hosts.size(); i++)
    {
        Host& h = t.hosts[i];
        std::sort(h.schedule.begin(), h.schedule.end());
        int64_t last = -1;
        for (auto& [when, msgId, size] : h.schedule)
        {
            if (when != last)
            {
                uint64_t key = Mix(t.seed ^ Mix(static_cast<uint64_t>(when)) ^ Mix(i + 1));
                offers.emplace_back(when, key, i);
                last = when;
            }
        }
    }
    std::sort(offers.begin(), offers.end());
    for (auto& [when, key, i] : offers)
    {
        Simulator::Schedule(NanoSeconds(when), &OnOffer, i);
    }

    Simulator::Stop(NanoSeconds(t.durationNs));
    Simulator::Run();

    // on_stop, in the same order as on_start. Anything sent from it is
    // counted but never arrives, the same as a real host shutting down.
    for (size_t i : order)
    {
        if (!t.hosts[i].role.empty())
        {
            Dispatch(t.hosts[i], "stop");
        }
    }
    t.current = nullptr;
    if (t.vis.is_open())
    {
        t.vis.close();
    }
    WriteStats(statsPath);
}

// ---- called by student code, through transportlab/api.py ------------------

double
SimNow()
{
    return static_cast<double>(NowNs()) / 1e9;
}

bool
Send(py::bytes data, const std::string& ip, int port)
{
    Host& h = Current();
    std::string bytes(data);
    if (bytes.size() > kMaxDatagram)
    {
        throw std::runtime_error("datagram exceeds " + std::to_string(kMaxDatagram) + " bytes");
    }
    Ipv4Address dst(ip.c_str());
    if (T().hostByIp.find(dst.Get()) == T().hostByIp.end() || port < 0 || port > 65535)
    {
        return false;
    }
    Ptr<Packet> p =
        Create<Packet>(reinterpret_cast<const uint8_t*>(bytes.data()), bytes.size());
    h.socket->SendTo(p, 0, InetSocketAddress(dst, static_cast<uint16_t>(port)));
    uint32_t wire = bytes.size() + kWireHeaderBytes;
    h.stats.datagramsSent++;
    h.stats.wireBytesSent += wire;
    h.stats.wireBuckets[NowNs() / T().bucketNs] += wire;
    return true;
}

void
SetTimer(const std::string& name, double delayS)
{
    Host& h = Current();
    if (!(delayS >= 0) || !std::isfinite(delayS))
    {
        throw std::runtime_error("timer delay must be a non-negative number");
    }
    auto old = h.timers.find(name);
    if (old != h.timers.end())
    {
        old->second.Cancel();
    }
    int64_t ns = std::llround(delayS * 1e9);
    size_t index = T().hostByName[h.name];
    h.timers[name] = Simulator::Schedule(NanoSeconds(ns), &OnTimer, index, name);
}

void
CancelTimer(const std::string& name)
{
    Host& h = Current();
    auto hit = h.timers.find(name);
    if (hit != h.timers.end())
    {
        hit->second.Cancel();
        h.timers.erase(hit);
    }
}

bool
TimerPending(const std::string& name)
{
    Host& h = Current();
    return h.timers.find(name) != h.timers.end();
}

// Same checks as Receiver.deliver in api.py. This count is the graded one.
std::string
Deliver(int64_t msgId, py::bytes payload)
{
    Host& h = Current();
    auto hit = h.expected.find(msgId);
    if (hit == h.expected.end())
    {
        h.stats.unknown++;
        return "unknown";
    }
    if (h.done.count(msgId))
    {
        h.stats.duplicates++;
        return "duplicate";
    }
    if (std::string(payload) != hit->second.payload)
    {
        h.stats.corrupt++;
        return "corrupt";
    }
    h.done.insert(msgId);
    int64_t now = NowNs();
    int64_t latency = now - hit->second.offeredNs;
    h.stats.delivered++;
    h.stats.appBytesDelivered += hit->second.size;
    if (hit->second.deadlineNs >= 0 && latency > hit->second.deadlineNs)
    {
        h.stats.late++;
    }
    h.stats.samples.push_back({msgId, latency, now});
    h.stats.appBuckets[now / T().bucketNs] += hit->second.size;
    return "ok";
}

// ---- results ----------------------------------------------------------------

// The graded counts, written by C++ straight to a file at the end of run(),
// after the last protocol callback. Python never holds them, so a protocol
// can't change them by patching the harness.
std::string
Quote(const std::string& in)
{
    std::string out = "\"";
    for (unsigned char c : in)
    {
        if (c == '"' || c == '\\')
        {
            out += '\\';
            out += static_cast<char>(c);
        }
        else if (c < 0x20)
        {
            char buf[8];
            std::snprintf(buf, sizeof buf, "\\u%04x", c);
            out += buf;
        }
        else
        {
            out += static_cast<char>(c);
        }
    }
    return out + "\"";
}

template <typename Map>
std::string
Buckets(const Map& m)
{
    std::ostringstream o;
    o << "{";
    bool first = true;
    for (auto& [k, v] : m)
    {
        o << (first ? "" : ",") << "\"" << k << "\":" << v;
        first = false;
    }
    o << "}";
    return o.str();
}

void
WriteStats(const std::string& path)
{
    Trial& t = T();
    std::ostringstream o;
    o << "{\"_label\":" << Quote(t.label) << ",\"_ns3_version\":" << Quote(TL_NS3_VERSION);
    o << ",\"_links\":{";
    bool firstLink = true;
    for (auto& link : t.links)
    {
        for (uint32_t w = 0; w < 2; w++)
        {
            std::string key = w == 0 ? link.a + "-" + link.b : link.b + "-" + link.a;
            Ptr<TlQueue> q = link.queue[w];
            o << (firstLink ? "" : ",") << Quote(key) << ":{"
              << "\"loss_drops\":" << q->lossDrops
              << ",\"flush_drops\":" << q->flushDrops
              << ",\"queue_drops\":" << q->GetTotalDroppedPackets() - q->lossDrops - q->flushDrops
              << ",\"flight_drops\":" << link.flightDrops[w]
              << ",\"sent\":" << q->GetTotalReceivedPackets() << "}";
            firstLink = false;
        }
    }
    o << "}";
    for (auto& h : t.hosts)
    {
        const HostStats& s = h.stats;
        o << "," << Quote(h.name) << ":{\"counters\":{"
          << "\"datagrams_sent\":" << s.datagramsSent
          << ",\"datagrams_recv\":" << s.datagramsRecv
          << ",\"wire_bytes_sent\":" << s.wireBytesSent
          << ",\"wire_bytes_recv\":" << s.wireBytesRecv
          << ",\"app_bytes_offered\":" << s.appBytesOffered
          << ",\"app_bytes_delivered\":" << s.appBytesDelivered
          << ",\"offered\":" << s.offered
          << ",\"delivered\":" << s.delivered
          << ",\"duplicates\":" << s.duplicates
          << ",\"corrupt\":" << s.corrupt
          << ",\"unknown\":" << s.unknown
          << ",\"late\":" << s.late << "}"
          << ",\"wire_buckets\":" << Buckets(s.wireBuckets)
          << ",\"app_buckets\":" << Buckets(s.appBuckets)
          << ",\"samples_ns\":[";
        for (size_t i = 0; i < s.samples.size(); i++)
        {
            const Sample& x = s.samples[i];
            o << (i ? "," : "") << "[" << x.msgId << "," << x.latencyNs << "," << x.tNs << "]";
        }
        o << "],\"expected\":" << h.expected.size()
          << ",\"unoffered\":" << h.schedule.size() - h.nextOffer
          << ",\"crashed\":" << (h.crashed ? "true" : "false")
          << ",\"error\":" << Quote(h.error) << "}";
    }
    o << "}\n";

    // Replace whatever is at the path, even a read-only file a protocol left
    // there, rather than writing through it.
    std::remove(path.c_str());
    std::ofstream f(path, std::ios::out | std::ios::trunc);
    f << o.str();
    f.close();
    if (!f)
    {
        throw std::runtime_error("cannot write " + path);
    }
}

void
SetLabel(const std::string& label)
{
    T().label = label;
}

std::string
Version()
{
    return TL_NS3_VERSION;
}

} // namespace

PYBIND11_MODULE(_tlsim, m)
{
    m.doc() = "ns-3 backend for transportlab. See sim/tlsim.cc.";
    // ns-3 records every Time in a registry until the first Simulator::Run.
    // A process that exits before one (the import check in setup) destroys
    // them after the registry's mutex, which aborts on macOS. Fixing the
    // resolution now, at its default of ns, empties the registry.
    Time::SetResolution(Time::NS);
    m.def("configure", &Configure);
    m.def("expect", &Expect);
    m.def("offer", &Offer);
    m.def("run", &Run);
    m.def("set_label", &SetLabel);
    m.def("now", &SimNow);
    m.def("send", &Send);
    m.def("set_timer", &SetTimer);
    m.def("cancel_timer", &CancelTimer);
    m.def("timer_pending", &TimerPending);
    m.def("deliver", &Deliver);
    m.def("ns3_version", &Version);
}
