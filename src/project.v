`default_nettype none
module tt_um_timed_protocol_sequencer(
    input wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input wire [7:0] uio_in,
    output wire [7:0] uio_out, uio_oe,
    input wire ena, clk, rst_n
);
    localparam EMPTY = 3'd0, LOAD = 3'd1, READY = 3'd2, RUN = 3'd3, FAULT = 3'd4;
    localparam POLL = 8'h00, BEGIN_LOAD = 8'h10, WORD = 8'h11, COMMIT = 8'h12,
               START = 8'h13, STOP = 8'h14, CONFIG = 8'h15, CLEAR = 8'h16,
               DIAG = 8'h17, COUNTS = 8'h18, PROGRESS = 8'h19,
               TX_WRITE = 8'h20, RX_READ = 8'h21, FLUSH = 8'h22;
    wire _unused = &{1'b0, ui_in[7]};
    wire ext_reset = !rst_n || !ena;
    reg [1:0] released;
    always @(posedge clk or posedge ext_reset)
        if (ext_reset) released <= 0;
        else released <= {released[0],1'b1};
    // Assertion propagates asynchronously through the cleared release flops.
    // E2 removes internal reset after its edge; E3 performs the first work.
    wire reset = !released[1];

    reg [14:0] f1, f2, h;
    always @(posedge clk or posedge reset) begin
        if (reset) begin
            f1 <= 15'h1000;
            f2 <= 15'h1000;
            h <= 15'h1000;
        end else begin
            f1 <= {ui_in[6:0],uio_in};
            f2 <= f1;
            h <= f2;
        end
    end

    reg armed, frame, miso;
    reg [5:0] bit_count;
    reg [4:0] reply_position;
    reg [31:0] shift, mailbox;
    wire cs = f2[12], sck = f2[13], mosi = f2[14];
    wire cs_change = cs ^ h[12];
    wire publish = armed && frame && cs_change && cs;
    wire [7:0] cmd = shift[31:24], a = shift[23:16];
    wire [15:0] arg_word = shift[15:0];
    // Index the stable mailbox instead of duplicating it in a shift register.
    // Track falls independently of rises; CS transitions take priority.
    wire [4:0] prior_bit = reply_position - 1'b1;
    always @(posedge clk or posedge reset) begin
        if (reset) begin
            {armed,frame,miso,reply_position,bit_count,shift} <= 0;
        end else if (!armed) begin
            armed <= cs && !sck;
        end else if (cs_change) begin
            frame <= !cs;
            if (!cs) begin
                bit_count <= 0;
                shift <= 0;
                reply_position <= 31;
                miso <= mailbox[31];
            end else miso <= 0;
        end else if (frame && sck != h[13]) begin
            if (sck) begin
                shift <= {shift[30:0],mosi};
                if (bit_count < 33) bit_count <= bit_count + 1'b1;
            end else if (reply_position != 0) begin
                reply_position <= prior_bit;
                miso <= mailbox[prior_bit];
            end else miso <= 0;
        end
    end

    reg [2:0] mode, next_mode;
    reg [1:0] cfg;
    reg poison;
    reg [7:0] accepted, od, pending_od, errors, detail;
    reg [15:0] crc;
    reg [15:0] imem [0:127];
    wire [6:0] pc;
    wire [15:0] instruction = imem[pc];
    wire [3:0] tx_count, rx_count, next_tx, next_rx;
    wire [7:0] tx_head, rx_head, r, engine_errors;
    wire pull, push, halt, fatal;
    wire active = mode == READY || mode == RUN;
    wire stoppable = active || mode == FAULT;
    reg args_ok, mode_ok;
    reg [2:0] rc;
    always @* begin
        args_ok = shift[23:0] == 0;
        mode_ok = 1;
        case (cmd)
            POLL,CLEAR,DIAG,COUNTS,PROGRESS: ;
            BEGIN_LOAD: args_ok = arg_word == 0;
            WORD: begin
                args_ok = 1;
                mode_ok = mode == LOAD;
            end
            COMMIT: begin
                args_ok = a == 0;
                mode_ok = mode == LOAD;
            end
            START: mode_ok = mode == READY;
            STOP,RX_READ: mode_ok = stoppable;
            CONFIG: begin
                args_ok = shift[23:2] == 0;
                mode_ok = mode == READY;
            end
            TX_WRITE: begin
                args_ok = shift[23:8] == 0;
                mode_ok = active;
            end
            FLUSH: mode_ok = mode == READY || mode == FAULT;
            default: args_ok = 0;
        endcase
        if (bit_count != 32) rc = 1;
        else if (!args_ok) rc = 2;
        else if (!mode_ok) rc = 3;
        else if (cmd == WORD && (poison || accepted[7] || a != accepted)) rc = 4;
        else if (cmd == COMMIT && (poison || accepted != 128)) rc = 4;
        else if (cmd == COMMIT && crc != arg_word) rc = 5;
        else if (cmd == TX_WRITE && tx_count == 8) rc = 6;
        else if (cmd == RX_READ && rx_count == 0) rc = 7;
        else rc = 0;
    end
    wire ok = publish && rc == 0;
    wire begin_load = ok && cmd == BEGIN_LOAD;
    wire start_run = ok && cmd == START;
    wire stop_run = ok && cmd == STOP;
    wire clear_exec = begin_load || start_run || stop_run;
    wire flush = begin_load || (ok && cmd == FLUSH);
    wire clear_errors = begin_load || (ok && cmd == CLEAR);
    wire execute = mode == RUN && !clear_exec;
    wire [3:0] o;
    sequencer_engine engine(
        .clk(clk), .reset(reset), .clear_exec(clear_exec), .run(execute),
        .word(instruction), .levels(f2[11:0]), .history(h[11:0]),
        .od(od), .tx_head(tx_head), .cfg(cfg), .tx_empty(tx_count == 0),
        .rx_full(rx_count == 8), .pc(pc), .p_data(uio_out), .p_oe(uio_oe),
        .r(r), .o(o), .pull(pull), .push(push), .halt(halt), .fatal(fatal),
        .error_bits(engine_errors)
    );
    sequencer_fifo tx_fifo(
        .clk(clk), .reset(reset), .flush(flush), .put(ok && cmd == TX_WRITE),
        .pop(pull), .data(shift[7:0]), .head(tx_head), .count(tx_count), .next_count(next_tx)
    );
    sequencer_fifo rx_fifo(
        .clk(clk), .reset(reset), .flush(flush), .put(push), .pop(ok && cmd == RX_READ),
        .data(r), .head(rx_head), .count(rx_count), .next_count(next_rx)
    );
    assign uo_out = {((|errors) || (|rx_count)), (mode == RUN), armed, miso, o};

    function [15:0] crc_byte;
        input [15:0] seed;
        input [7:0] data;
        integer n;
        reg [15:0] acc;
        begin
            acc = seed ^ {data,8'h00};
            for (n = 0; n < 8; n = n + 1)
                acc = {acc[14:0],1'b0} ^ (acc[15] ? 16'h1021 : 16'h0000);
            crc_byte = acc;
        end
    endfunction
    integer i;
    always @(posedge clk or posedge reset) begin
        if (reset) begin
            for (i = 0; i < 128; i = i + 1) imem[i] <= 0;
            {od,pending_od,accepted,crc,poison} <= 0;
            cfg <= 3;
        end else if (begin_load) begin
            od <= 0;
            pending_od <= a;
            accepted <= 0;
            crc <= crc_byte(16'hffff,a);
            poison <= 0;
            cfg <= 3;
        end else begin
            if (publish && rc != 0 && mode == LOAD) poison <= 1;
            if (ok && cmd == WORD) begin
                imem[accepted[6:0]] <= arg_word;
                accepted <= accepted + 1'b1;
                crc <= crc_byte(crc_byte(crc,arg_word[15:8]),arg_word[7:0]);
            end
            if (ok && cmd == COMMIT) od <= pending_od;
            if (ok && cmd == CONFIG) cfg <= shift[1:0];
        end
    end

    // Data bytes sample old state. STATUS describes both transactions after
    // the edge, even if a host rejection coincides with an engine fault.
    always @* begin
        next_mode = mode;
        if (begin_load) next_mode = LOAD;
        else if (start_run) next_mode = RUN;
        else if (stop_run || (ok && cmd == COMMIT) || halt) next_mode = READY;
        else if (fatal) next_mode = FAULT;
    end
    wire [7:0] host_errors = (!publish || rc == 0) ? 8'h00 :
                             rc <= 3 ? 8'h01 : rc <= 5 ? 8'h02 : 8'h04;
    wire [7:0] next_errors = (clear_errors ? 8'h00 : errors) | host_errors | engine_errors;
    wire next_valid = next_mode == READY || next_mode == RUN || next_mode == FAULT;
    wire [7:0] status = {(next_mode == LOAD),next_valid,(|next_errors),(next_mode == RUN),
                         (next_rx == 8),(next_rx == 0),(next_tx == 8),(next_tx == 0)};
    reg [15:0] reply_data;
    always @* begin
        reply_data = 0;
        if (rc == 0) begin
            case (cmd)
                POLL: reply_data = {5'd0,mode,6'd0,cfg};
                WORD: reply_data = {8'd0,accepted + 8'd1};
                DIAG: reply_data = {detail,errors};
                COUNTS: reply_data = {4'd0,tx_count,4'd0,rx_count};
                PROGRESS: reply_data = {1'b0,pc,accepted};
                RX_READ: reply_data = {8'd0,rx_head};
                default: reply_data = 0;
            endcase
        end
    end
    always @(posedge clk or posedge reset) begin
        if (reset) begin
            mode <= EMPTY;
            {errors,detail} <= 0;
            mailbox <= 32'h00050000;
        end else begin
            mode <= next_mode;
            errors <= next_errors;
            if (engine_errors[7]) detail <= instruction[7:0];
            else if (clear_errors) detail <= 0;
            if (publish) mailbox <= {5'd0,rc,status,reply_data};
        end
    end
endmodule

// Entry point named by the frozen Tiny Tapeout metadata.
module tt_um_kunal_timed_protocol_sequencer(
    input wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input wire [7:0] uio_in,
    output wire [7:0] uio_out, uio_oe,
    input wire ena, clk, rst_n
);
    tt_um_timed_protocol_sequencer core(
        .ui_in(ui_in), .uo_out(uo_out), .uio_in(uio_in),
        .uio_out(uio_out), .uio_oe(uio_oe), .ena(ena), .clk(clk), .rst_n(rst_n)
    );
endmodule
`default_nettype wire
