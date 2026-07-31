CC ?= cc

CPPFLAGS += -DPGLC_RESP_STANDALONE -I../../src
COMMON_CFLAGS = -std=c11 -O2 -g -Wall -Wextra -Werror -Wpedantic
SOURCES = resp_test.c ../../src/resp.c
TARGET = resp_test
SANITIZED_TARGET = resp_test_sanitized

.PHONY: all check sanitize clean

all: $(TARGET)

$(TARGET): $(SOURCES) ../../src/resp.h ../../src/resp_limits.h
	$(CC) $(CPPFLAGS) $(COMMON_CFLAGS) $(CFLAGS) $(SOURCES) $(LDFLAGS) -o $@

$(SANITIZED_TARGET): $(SOURCES) ../../src/resp.h ../../src/resp_limits.h
	$(CC) $(CPPFLAGS) $(COMMON_CFLAGS) -O1 \
		-fsanitize=address,undefined -fno-omit-frame-pointer \
		$(CFLAGS) $(SOURCES) $(LDFLAGS) \
		-fsanitize=address,undefined -o $@

check: $(TARGET)
	./$(TARGET)

sanitize: $(SANITIZED_TARGET)
	ASAN_OPTIONS=$${ASAN_OPTIONS:-detect_leaks=0:strict_string_checks=1} \
	UBSAN_OPTIONS=$${UBSAN_OPTIONS:-print_stacktrace=1:halt_on_error=1} \
	./$(SANITIZED_TARGET)

clean:
	rm -f $(TARGET) $(SANITIZED_TARGET)
