/*
 * Minimal ESP32-H2 Thread/IPv6 USB bridge for a directly attached Matter host.
 *
 * The H2 owns the OpenThread stack and forms the Thread network.  The USB link
 * carries complete IPv6 datagrams using SLIP framing; it is deliberately not a
 * border router and has no infrastructure-network interface.
 */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "driver/usb_serial_jtag.h"
#include "esp_check.h"
#include "esp_event.h"
#include "esp_openthread.h"
#include "esp_openthread_lock.h"
#include "esp_openthread_types.h"
#include "esp_vfs_eventfd.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "openthread/dataset.h"
#include "openthread/dataset_ftd.h"
#include "openthread/ip6.h"
#include "openthread/message.h"
#include "openthread/thread.h"
#include "openthread/thread_ftd.h"

#define SLIP_END 0xc0
#define SLIP_ESC 0xdb
#define SLIP_ESC_END 0xdc
#define SLIP_ESC_ESC 0xdd

#define FRAME_IPV6_TO_H2 0x01
#define FRAME_IPV6_TO_HOST 0x02
#define FRAME_STATUS_REQUEST 0x10
#define FRAME_STATUS_RESPONSE 0x11
#define PROTOCOL_VERSION 6
#define IPV6_MTU 1280
#define MAX_REPORTED_PEERS 32
#define PEER_FLAG_ROUTER 0x01

typedef struct {
    uint16_t length;
    uint8_t data[];
} bridge_frame_t;

static QueueHandle_t s_tx_queue;
static uint32_t s_injected_packets;
static uint32_t s_rejected_packets;
static uint32_t s_received_packets;
static uint8_t s_last_inject_error;

static void queue_frame(uint8_t type, const uint8_t *payload, uint16_t length)
{
    bridge_frame_t *frame = malloc(sizeof(*frame) + length + 1);
    if (frame == NULL) {
        return;
    }
    frame->length = length + 1;
    frame->data[0] = type;
    if (length != 0) {
        memcpy(frame->data + 1, payload, length);
    }
    if (xQueueSend(s_tx_queue, &frame, 0) != pdTRUE) {
        free(frame);
    }
}

static void thread_receive(otMessage *message, void *context)
{
    (void)context;
    uint16_t length = otMessageGetLength(message);
    s_received_packets++;
    if (length <= IPV6_MTU) {
        uint8_t packet[IPV6_MTU];
        if (otMessageRead(message, 0, packet, length) == length) {
            queue_frame(FRAME_IPV6_TO_HOST, packet, length);
        }
    }
    otMessageFree(message);
}

static void send_status(void)
{
    otOperationalDatasetTlvs tlvs = {0};
    uint8_t payload[3 + 2 * sizeof(otIp6Address) + OT_OPERATIONAL_DATASET_MAX_LENGTH +
                    18 + MAX_REPORTED_PEERS * 11];
    otInstance *instance;
    const otIp6Address *mleid;
    const otIp6Address *link_local;
    uint8_t child_count = 0;
    uint8_t router_neighbor_count = 0;
    uint16_t peer_rloc16 = 0xffff;
    uint8_t peer_count = 0;
    otExtAddress peer_extaddrs[MAX_REPORTED_PEERS];
    uint16_t peer_rlocs[MAX_REPORTED_PEERS];
    uint8_t peer_flags[MAX_REPORTED_PEERS];

    esp_openthread_lock_acquire(portMAX_DELAY);
    instance = esp_openthread_get_instance();
    otError error = otDatasetGetActiveTlvs(instance, &tlvs);
    mleid = otThreadGetMeshLocalEid(instance);
    link_local = otThreadGetLinkLocalIp6Address(instance);

    payload[0] = PROTOCOL_VERSION;
    payload[1] = (uint8_t)otThreadGetDeviceRole(instance);
    payload[2] = error == OT_ERROR_NONE ? tlvs.mLength : 0;
    memcpy(payload + 3, mleid, sizeof(*mleid));
    if (error == OT_ERROR_NONE) {
        memcpy(payload + 3 + sizeof(*mleid), tlvs.mTlvs, tlvs.mLength);
    }
    otChildInfo child;
    while (otThreadGetChildInfoByIndex(instance, child_count, &child) == OT_ERROR_NONE) {
        if (peer_rloc16 == 0xffff) {
            peer_rloc16 = child.mRloc16;
        }
        if (peer_count < MAX_REPORTED_PEERS) {
            peer_extaddrs[peer_count] = child.mExtAddress;
            peer_rlocs[peer_count] = child.mRloc16;
            peer_flags[peer_count] = 0;
            peer_count++;
        }
        child_count++;
    }
    uint16_t self_rloc16 = otThreadGetRloc16(instance);
    if (otThreadGetDeviceRole(instance) == OT_DEVICE_ROLE_CHILD &&
        peer_count < MAX_REPORTED_PEERS) {
        otRouterInfo parent;
        if (otThreadGetParentInfo(instance, &parent) == OT_ERROR_NONE) {
            peer_extaddrs[peer_count] = parent.mExtAddress;
            peer_rlocs[peer_count] = parent.mRloc16;
            peer_flags[peer_count] = PEER_FLAG_ROUTER;
            peer_count++;
            peer_rloc16 = parent.mRloc16;
            router_neighbor_count++;
        }
    }
    for (uint8_t router_id = 0; router_id <= OT_NETWORK_MAX_ROUTER_ID; router_id++) {
        otRouterInfo router;
        if (otThreadGetRouterInfo(instance, router_id, &router) == OT_ERROR_NONE &&
            router.mAllocated && router.mRloc16 != self_rloc16) {
            if (router.mLinkEstablished) {
                router_neighbor_count++;
            }
            if (peer_rloc16 == 0xffff) {
                peer_rloc16 = router.mRloc16;
            }
            bool already_reported = false;
            bool extaddr_is_zero = true;
            for (size_t i = 0; i < sizeof(router.mExtAddress.m8); i++) {
                extaddr_is_zero &= router.mExtAddress.m8[i] == 0;
            }
            for (uint8_t i = 0; i < peer_count; i++) {
                already_reported |= peer_rlocs[i] == router.mRloc16;
            }
            if (!already_reported && !extaddr_is_zero && peer_count < MAX_REPORTED_PEERS) {
                peer_extaddrs[peer_count] = router.mExtAddress;
                peer_rlocs[peer_count] = router.mRloc16;
                peer_flags[peer_count] = PEER_FLAG_ROUTER;
                peer_count++;
            }
        }
    }
    size_t offset = 3 + sizeof(*mleid) + payload[2];
    memcpy(payload + offset, link_local, sizeof(*link_local));
    offset += sizeof(*link_local);
    memcpy(payload + offset, &s_injected_packets, sizeof(s_injected_packets));
    offset += sizeof(s_injected_packets);
    memcpy(payload + offset, &s_rejected_packets, sizeof(s_rejected_packets));
    offset += sizeof(s_rejected_packets);
    memcpy(payload + offset, &s_received_packets, sizeof(s_received_packets));
    offset += sizeof(s_received_packets);
    payload[offset++] = s_last_inject_error;
    payload[offset++] = child_count;
    payload[offset++] = router_neighbor_count;
    memcpy(payload + offset, &peer_rloc16, sizeof(peer_rloc16));
    offset += sizeof(peer_rloc16);
    payload[offset++] = peer_count;
    for (uint8_t i = 0; i < peer_count; i++) {
        memcpy(payload + offset, peer_extaddrs[i].m8, sizeof(peer_extaddrs[i].m8));
        offset += sizeof(peer_extaddrs[i].m8);
        memcpy(payload + offset, &peer_rlocs[i], sizeof(peer_rlocs[i]));
        offset += sizeof(peer_rlocs[i]);
        payload[offset++] = peer_flags[i];
    }
    esp_openthread_lock_release();

    queue_frame(FRAME_STATUS_RESPONSE, payload, offset);
}

static void inject_ipv6(const uint8_t *packet, uint16_t length)
{
    if (length < 40 || length > IPV6_MTU || (packet[0] >> 4) != 6) {
        return;
    }

    esp_openthread_lock_acquire(portMAX_DELAY);
    otInstance *instance = esp_openthread_get_instance();
    otMessage *message = otIp6NewMessageFromBuffer(instance, packet, length, NULL);
    if (message != NULL) {
        /* otIp6Send takes ownership, including on failure. */
        otError error = otIp6Send(instance, message);
        if (error == OT_ERROR_NONE) {
            s_injected_packets++;
        } else {
            s_rejected_packets++;
            s_last_inject_error = (uint8_t)error;
        }
    } else {
        s_rejected_packets++;
        s_last_inject_error = (uint8_t)OT_ERROR_NO_BUFS;
    }
    esp_openthread_lock_release();
}

static void handle_host_frame(const uint8_t *frame, uint16_t length)
{
    if (length == 0) {
        return;
    }
    switch (frame[0]) {
    case FRAME_IPV6_TO_H2:
        inject_ipv6(frame + 1, length - 1);
        break;
    case FRAME_STATUS_REQUEST:
        send_status();
        break;
    default:
        break;
    }
}

static void usb_rx_task(void *arg)
{
    (void)arg;
    uint8_t frame[IPV6_MTU + 1];
    uint16_t length = 0;
    bool escaped = false;
    bool overflow = false;

    for (;;) {
        uint8_t input[128];
        int count = usb_serial_jtag_read_bytes(input, sizeof(input), pdMS_TO_TICKS(100));
        for (int i = 0; i < count; ++i) {
            uint8_t byte = input[i];
            if (byte == SLIP_END) {
                if (!overflow && length != 0) {
                    handle_host_frame(frame, length);
                }
                length = 0;
                escaped = false;
                overflow = false;
                continue;
            }
            if (overflow) {
                continue;
            }
            if (escaped) {
                if (byte == SLIP_ESC_END) {
                    byte = SLIP_END;
                } else if (byte == SLIP_ESC_ESC) {
                    byte = SLIP_ESC;
                } else {
                    length = 0;
                    overflow = true;
                    continue;
                }
                escaped = false;
            } else if (byte == SLIP_ESC) {
                escaped = true;
                continue;
            }
            if (length == sizeof(frame)) {
                length = 0;
                overflow = true;
            } else {
                frame[length++] = byte;
            }
        }
    }
}

static void usb_write_all(const uint8_t *data, size_t length)
{
    while (length != 0) {
        int written = usb_serial_jtag_write_bytes(data, length, pdMS_TO_TICKS(1000));
        if (written > 0) {
            data += written;
            length -= written;
        }
    }
}

static void usb_tx_task(void *arg)
{
    (void)arg;
    uint8_t encoded[2 * (IPV6_MTU + 1) + 2];
    for (;;) {
        bridge_frame_t *frame;
        if (xQueueReceive(s_tx_queue, &frame, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        size_t output_length = 0;
        encoded[output_length++] = SLIP_END;
        for (uint16_t i = 0; i < frame->length; ++i) {
            uint8_t byte = frame->data[i];
            if (byte == SLIP_END) {
                encoded[output_length++] = SLIP_ESC;
                encoded[output_length++] = SLIP_ESC_END;
            } else if (byte == SLIP_ESC) {
                encoded[output_length++] = SLIP_ESC;
                encoded[output_length++] = SLIP_ESC_ESC;
            } else {
                encoded[output_length++] = byte;
            }
        }
        encoded[output_length++] = SLIP_END;
        usb_write_all(encoded, output_length);
        free(frame);
    }
}

static void subscribe_mdns(otInstance *instance, const char *address)
{
    otIp6Address multicast;
    if (otIp6AddressFromString(address, &multicast) == OT_ERROR_NONE) {
        otError error = otIp6SubscribeMulticastAddress(instance, &multicast);
        if (error != OT_ERROR_NONE && error != OT_ERROR_ALREADY) {
            abort();
        }
    }
}

static void start_thread_network(void)
{
    otInstance *instance = esp_openthread_get_instance();
    otOperationalDatasetTlvs tlvs;

    if (otDatasetGetActiveTlvs(instance, &tlvs) != OT_ERROR_NONE) {
        otOperationalDataset dataset;
        ESP_ERROR_CHECK(otDatasetCreateNewNetwork(instance, &dataset) == OT_ERROR_NONE
                            ? ESP_OK
                            : ESP_FAIL);
        ESP_ERROR_CHECK(otDatasetSetActive(instance, &dataset) == OT_ERROR_NONE
                            ? ESP_OK
                            : ESP_FAIL);
    }

    otIp6SetReceiveCallback(instance, thread_receive, NULL);
    otIp6SetReceiveFilterEnabled(instance, true);
    subscribe_mdns(instance, "ff02::fb");
    subscribe_mdns(instance, "ff03::fb");
    ESP_ERROR_CHECK(otIp6SetEnabled(instance, true) == OT_ERROR_NONE ? ESP_OK
                                                                    : ESP_FAIL);
    ESP_ERROR_CHECK(otThreadSetEnabled(instance, true) == OT_ERROR_NONE ? ESP_OK
                                                                        : ESP_FAIL);
}

void app_main(void)
{
    esp_err_t nvs_error = nvs_flash_init();
    if (nvs_error == ESP_ERR_NVS_NO_FREE_PAGES ||
        nvs_error == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    } else {
        ESP_ERROR_CHECK(nvs_error);
    }
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    esp_vfs_eventfd_config_t eventfd_config = {.max_fds = 2};
    ESP_ERROR_CHECK(esp_vfs_eventfd_register(&eventfd_config));

    usb_serial_jtag_driver_config_t usb_config = {
        .tx_buffer_size = 2048,
        .rx_buffer_size = 2048,
    };
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usb_config));
    s_tx_queue = xQueueCreate(8, sizeof(bridge_frame_t *));
    assert(s_tx_queue != NULL);

    static const esp_openthread_platform_config_t ot_config = {
        .radio_config = {.radio_mode = RADIO_MODE_NATIVE},
        .host_config = {.host_connection_mode = HOST_CONNECTION_MODE_NONE},
        .port_config = {
            .storage_partition_name = "nvs",
            .netif_queue_size = 4,
            .task_queue_size = 8,
        },
    };
    ESP_ERROR_CHECK(esp_openthread_init(&ot_config));

    esp_openthread_lock_acquire(portMAX_DELAY);
    start_thread_network();
    esp_openthread_lock_release();

    assert(xTaskCreate(usb_rx_task, "usb_rx", 8192, NULL, 4, NULL) == pdPASS);
    assert(xTaskCreate(usb_tx_task, "usb_tx", 4096, NULL, 4, NULL) == pdPASS);

    /* The OpenThread mainloop remains on app_main's task. */
    ESP_ERROR_CHECK(esp_openthread_launch_mainloop());
}
