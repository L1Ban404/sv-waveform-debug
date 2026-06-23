module elaboration_leaf #(parameter int WIDTH = 8) (
  output logic [WIDTH-1:0] value
);
endmodule

module elaboration_top;
  elaboration_leaf #(.WIDTH(13)) u_leaf ();
endmodule
