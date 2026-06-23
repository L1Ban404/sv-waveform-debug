module pulse(input logic clk, input logic rst_n, output logic valid_o);
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) valid_o <= 1'b0;
    else valid_o <= ~valid_o;
  end
endmodule

module top_tb;
  logic clk;
  logic rst_n;
  logic valid_o;
  pulse u_dut (.*);
endmodule
