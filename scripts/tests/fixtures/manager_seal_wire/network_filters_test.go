package filters

import "testing"

// Compile with the UNMODIFIED pinned Moby parse.go and errors.go. No module,
// network dependency or substitute decoder. The Python suite skips explicitly
// when Go is unavailable; the real daemon canary is a separate queued gate.
func TestPinnedSDKAndDaemonDecoder(t *testing.T) {
	name := "agent-synthetic_migrated_agent-synthetic-seal"
	sdk, err := ToJSON(NewArgs(Arg("name", name)))
	if err != nil || sdk != `{"name":{"`+name+`":true}}` {
		t.Fatalf("unexpected pinned SDK serialization: %q %v", sdk, err)
	}
	zero := "0000000000000000000000000000000000000000000000000000000000000000"
	old := `{"name":{"`+name+`":true},"id":["`+zero+`"]}`
	fixed := `{"id":{"`+zero+`":true},"name":{"`+name+`":true}}`
	if _, err = FromJSON(old); err == nil || err.Error() != "invalid filter" {
		t.Fatalf("old mixed wire must fail BOTH complete decoders: %v", err)
	}
	args, err := FromJSON(fixed)
	if err != nil || !args.ExactMatch("id", zero) || !args.ExactMatch("name", name) {
		t.Fatalf("canonical query must decode without dropping selectors: %v", err)
	}
	if _, err = FromJSON(`{"name":["`+name+`"],"id":["`+zero+`"]}`); err != nil {
		t.Fatalf("homogeneous legacy query remains decodable: %v", err)
	}
}
