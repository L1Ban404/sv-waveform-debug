module handshake(input logic clk, input logic rst_n, input logic valid, ready,
                 input logic [7:0] data);
  assert property (@(posedge clk) disable iff (!rst_n)
                   valid && !ready |=> valid && $stable(data));
endmodule
